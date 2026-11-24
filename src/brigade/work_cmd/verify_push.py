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
from dataclasses import asdict, dataclass
from functools import cache, cached_property, partial
from pathlib import Path
from queue import Empty, Queue
from threading import Thread
from time import monotonic
from typing import Any

from .. import verify_manifest_git
from . import verify_gate
from .verify_push_input import INPUT_BYTES, branch_updates

_BUDGET_SECONDS = 2.0
_RECEIPT_LIMIT = 128
_RECEIPT_BYTES = 4 * 1024 * 1024
_TIMEOUT = "The push check reached its two-second time limit."


@dataclass(frozen=True)
class _Verdict:
    status: str
    reason: str | None = None
    remedy: str | None = None


class _Indeterminate(Exception):
    pass


def _remaining(deadline: float) -> float:
    remaining = deadline - monotonic()
    if remaining <= 0:
        raise _Indeterminate(_TIMEOUT)
    return remaining


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
        raise _Indeterminate(_TIMEOUT) from None
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
            key: data.get(key)
            for key in ("status", "tree_stable", "tree_fingerprint", "started_at", "gating")
            if key in data
        }

    def latest(self, tree: str) -> dict[str, Any] | None:
        latest: dict[str, Any] | None = None
        latest_second = ""
        for name in self.names[:_RECEIPT_LIMIT]:
            second = name[:15]
            if latest is not None and second != latest_second:
                return latest
            if name not in self.loaded:
                self.loaded[name] = self._load(name)
            receipt = self.loaded[name]
            if receipt is None or receipt.get("status") in ("running", "busy", "canceled"):
                continue
            stamp = receipt.get("gating")
            plan = stamp.get("plan") if isinstance(stamp, dict) else None
            # Closeout's comparison allows changes under .brigade/ and uses a
            # separate Git timeout. Push receipts must identify the exact committed tree.
            if receipt.get("tree_fingerprint") != tree and (
                not isinstance(plan, dict) or plan.get("snapshot_tree") != tree
            ):
                continue
            if latest is None or str(receipt.get("started_at") or "") > str(latest.get("started_at") or ""):
                latest = receipt
                latest_second = second
        if len(self.names) > _RECEIPT_LIMIT and (latest is None or self.names[-1][:15] == latest_second):
            raise _Indeterminate("The local receipt scan reached its limit. Run verification for the pushed tree.")
        return latest


def _remedy(target: Path, policy: verify_gate._GatePolicy, receipt: dict[str, Any]) -> str:
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
    if policy.needs_arguments:
        command += "\nFind the required test files with brigade work verify required --worktree."
    return command


def _tree_verdict(
    target: Path, policy: verify_gate._GatePolicy, tree: str, receipts: _Receipts, deadline: float
) -> _Verdict:
    receipt = receipts.latest(tree)
    _remaining(deadline)
    if receipt is None:
        return _Verdict(
            "missing_receipt", "No local verification receipt matches this tree.", _remedy(target, policy, {})
        )
    eligible, reason = verify_gate.evaluate_receipt(policy, receipt, tree, deadline=deadline)
    if eligible:
        return _Verdict("ok")
    if reason == "policy_timeout":
        raise _Indeterminate(_TIMEOUT)
    explanation, _ = verify_gate._plan_guidance(receipt, reason)
    return _Verdict(
        "rejected",
        explanation or f"The last run has an ineligible verdict ({reason.replace('_', ' ')}).",
        _remedy(target, policy, receipt),
    )


def _check(target: Path, updates: list[tuple[str, str]], ref: str | None, deadline: float) -> list[dict[str, Any]]:
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
        verdict = _Verdict("indeterminate", _TIMEOUT if isinstance(exc, subprocess.TimeoutExpired) else str(exc))
        names: list[str | None] = [name for name, _ in updates] if updates else [ref]
        return [{"ref": name, **asdict(verdict)} for name in names]

    receipts = _Receipts(target, deadline)
    by_commit: dict[str, _Verdict] = {}
    by_tree: dict[tuple[verify_gate._GatePolicy, str], _Verdict] = {}

    bases = verify_manifest_git._BaseResolver(target, deadline=deadline, git_env=git_env)

    @cache
    def committed_policy(commit: str) -> verify_gate._GatePolicy | None:
        return verify_gate._committed_policy(target, commit, deadline, git_env=git_env)

    def check_tip(commit: str) -> _Verdict:
        _remaining(deadline)
        base = bases.resolve(commit)
        if base.status != "resolved" or base.commit is None:
            raise _Indeterminate(base.message or "The trusted Git base is unresolved.")
        if base.default_branch_tip is None:
            raise _Indeterminate("Git could not find a fetched default branch tip.")
        policy = committed_policy(base.commit)
        if policy is None:
            tip = base.default_branch_tip
            tip_policy = committed_policy(tip) if tip != base.commit else None
            if tip_policy is None:
                return _Verdict("not_applicable")
            if tip_policy.mode == "unresolved":
                raise _Indeterminate(verify_gate._policy_error(tip_policy))
            return _Verdict(
                "base_predates_policy", f"Update the branch from {base.reference} to pick up the required check."
            )
        if policy.mode == "unresolved":
            raise _Indeterminate(verify_gate._policy_error(policy))
        resolved_tree = git(target, "rev-parse", "--verify", f"{commit}^{{commit}}^{{tree}}")
        if resolved_tree.returncode:
            raise _Indeterminate("Git could not read the pushed commit's tree.")
        tree = resolved_tree.stdout.decode("ascii").strip()
        key = (policy, tree)
        if key not in by_tree:
            by_tree[key] = _tree_verdict(target, policy, tree, receipts, deadline)
        return by_tree[key]

    results = []
    for name, commit in updates:
        if commit not in by_commit:
            try:
                by_commit[commit] = check_tip(commit)
            except subprocess.TimeoutExpired:
                by_commit[commit] = _Verdict("indeterminate", _TIMEOUT)
            except (OSError, _Indeterminate) as exc:
                by_commit[commit] = _Verdict("indeterminate", str(exc))
        results.append({"ref": name, **asdict(by_commit[commit])})
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
        results = [{"ref": ref, **asdict(_Verdict("indeterminate", str(exc)))}]

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
