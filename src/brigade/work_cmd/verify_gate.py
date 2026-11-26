"""Verdicts for the repository's required local verification check."""

from __future__ import annotations

import hashlib
import json
import re
import shlex
import stat
import subprocess
import unicodedata
from collections.abc import Iterable, Iterator, Mapping
from dataclasses import dataclass, replace
from datetime import datetime
from functools import cached_property, partial
from pathlib import Path
from time import monotonic
from typing import Any, Literal

from .. import localio, verify_manifest_git
from . import verify_required

_DECLARATION_PATH = "verify/required-check.json"
_DECLARATION_SCHEMA = "brigade.verify_required_check.v1"
STOP_POLICY_BUDGET_SECONDS = 2.0
_CLOSEOUT_POLICY_BUDGET_SECONDS = 60.0
_HEAD_FALLBACK_BUDGET_SECONDS = 1.0
# The operator chose this limit on 2026-11-22 to avoid long local runs.
_MAX_LOCAL_TEST_FILES = 20
_PLAN_SCHEMA_VERSION = 1
_TEST_ENV_OVERRIDES = {"PYTEST_ADDOPTS", "PYTEST_PLUGINS", "PYTHONPATH"}
_SINGLE_OPTIONS = {"-v", "-vv"}
_NUMBER_OPTIONS = {"-n", "--timeout", "--durations"}
_TRACEBACK_STYLES = {"auto", "long", "short", "line", "native", "no"}


@dataclass(frozen=True)
class _GatePolicy:
    mode: Literal["required_check", "audit_only", "unresolved"]
    reason: str
    argv_prefix: tuple[str, ...] = ()
    needs_arguments: bool = False
    needs_passed_tests: bool = False
    needs_plan_coverage: bool = False
    base_commit: str | None = None
    declaration_sha256: str | None = None
    fallback_source: Literal["default_branch_tip", "head", "worktree", "none"] | None = None
    base_message: str | None = None
    error: str | None = None

    def matches(self, stamp: Mapping[str, Any], current_tree: str | None) -> bool:
        plan = stamp.get("plan")
        return (
            stamp.get("mode") == self.mode
            and stamp.get("declaration_sha256") == self.declaration_sha256
            and stamp.get("fallback_source") == self.fallback_source
            and (
                not self.needs_plan_coverage
                or (
                    isinstance(plan, dict)
                    and plan.get("schema_version") == _PLAN_SCHEMA_VERSION
                    and self.base_commit is not None
                    and self.fallback_source is None
                    and plan.get("base_commit") == self.base_commit
                    and current_tree is not None
                    and plan.get("snapshot_tree") == current_tree
                )
            )
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
    needs_plan_coverage = payload.get("needs_plan_coverage", False)
    if (
        not isinstance(prefix, list)
        or not prefix
        or not all(isinstance(arg, str) for arg in prefix)
        or not prefix[0].strip()
    ):
        return _GatePolicy("unresolved", "declaration_invalid", error="argv_prefix must be a nonempty command array")
    if any(char == "`" or unicodedata.category(char) in {"Cc", "Zl", "Zp"} for arg in prefix for char in arg):
        return _GatePolicy(
            "unresolved",
            "declaration_invalid",
            error="argv_prefix cannot contain backticks, line breaks, or control characters",
        )
    if not isinstance(needs_arguments, bool) or not isinstance(needs_passed_tests, bool):
        return _GatePolicy(
            "unresolved", "declaration_invalid", error="needs_arguments and needs_passed_tests must be booleans"
        )
    if not isinstance(needs_plan_coverage, bool):
        return _GatePolicy("unresolved", "declaration_invalid", error="needs_plan_coverage must be a boolean")
    if needs_plan_coverage and not (needs_arguments and needs_passed_tests):
        return _GatePolicy(
            "unresolved",
            "declaration_invalid",
            error="needs_plan_coverage requires needs_arguments and needs_passed_tests",
        )
    return _GatePolicy(
        "required_check",
        "passed_required_check",
        tuple(prefix),
        needs_arguments,
        needs_passed_tests,
        needs_plan_coverage,
        base_commit=commit,
        declaration_sha256=hashlib.sha256(raw).hexdigest(),
    )


def _unreadable(detail: str) -> _GatePolicy:
    return _GatePolicy("unresolved", "declaration_unreadable", error=detail)


def _committed_policy(
    target: Path, commit: str, deadline: float | None, *, git_env: dict[str, str] | None = None
) -> _GatePolicy | None:
    git = verify_manifest_git._git if git_env is None else partial(verify_manifest_git._git, git_env=git_env)
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


def _fallback_policy(
    target: Path, deadline: float | None, base_message: str | None, *, default_branch_tip: str | None = None
) -> _GatePolicy:
    try:
        if default_branch_tip is not None:
            policy = _committed_policy(target, default_branch_tip, deadline)
            if policy is not None:
                return replace(policy, fallback_source="default_branch_tip", base_message=base_message)
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
        return _fallback_policy(target, deadline, base.message, default_branch_tip=base.default_branch_tip)
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


def _coverage_path(arg: str) -> str | None:
    path = arg.removeprefix("./")
    if verify_required._repo_path(path) and not path.startswith("@") and not any(c in path for c in "*?[]"):
        return path
    return None


def _command_files(args: list[str], entries: Mapping[str, tuple[bytes, bytes]]) -> set[str]:
    files: set[str] = set()
    values = iter(args)
    parse_options = True
    for arg in values:
        if parse_options and arg.startswith("-"):
            if arg == "--":
                parse_options = False
                continue
            if arg in _SINGLE_OPTIONS or (arg == "-p" and next(values, None) == "xdist"):
                continue
            option, separator, value = arg.partition("=")
            if option not in _NUMBER_OPTIONS and option != "--tb":
                return set()
            value = value if separator else next(values, "")
            if option == "--tb":
                if value not in _TRACEBACK_STYLES:
                    return set()
            elif not re.fullmatch(r"[0-9]+(?:\.[0-9]+)?" if option == "--timeout" else r"[0-9]+", value):
                return set()
            continue
        path = _coverage_path(arg)
        if path is None or path not in entries or not verify_required._regular(entries[path][0]):
            return set()
        files.add(path)
    return files


def _covered_files(
    receipt: Mapping[str, Any], policy: _GatePolicy, entries: Mapping[str, tuple[bytes, bytes]]
) -> set[str]:
    inherited = receipt.get("inherited_test_env") or {}
    if inherited.get("pythonpath_set") or _TEST_ENV_OVERRIDES.intersection(inherited):
        return set()
    covered: set[str] = set()
    prefix = list(policy.argv_prefix)
    for record in receipt.get("commands", []):
        argv = record.get("argv")
        if (
            isinstance(argv, list)
            and argv[: len(prefix)] == prefix
            and all(isinstance(arg, str) for arg in argv)
            and not _TEST_ENV_OVERRIDES.intersection(name.upper() for name in record.get("env", []))
            and _tests_passed(record)
            and record["test_outcomes"].get("deselected") == 0
        ):
            covered.update(_command_files(argv[len(prefix) :], entries))
    return covered


def _stamp_plan(target: Path, receipt: Mapping[str, Any], policy: _GatePolicy, stamp: dict[str, Any]) -> None:
    base = policy.base_commit if policy.fallback_source is None else None
    tree = receipt.get("tree_fingerprint")
    plan: dict[str, Any] = {
        "schema_version": _PLAN_SCHEMA_VERSION,
        "mode": "not_checked",
        "base_commit": base,
        "snapshot_tree": tree,
        "required_files": [],
        "missing_files": [],
        "run_argv": [],
    }
    stamp["plan"] = plan
    if _blocked_reason(receipt):
        return
    if receipt.get("reused_from"):
        stamp.update(eligible=False, reason="missing_gating")
        return
    if base is None:
        stamp.update(eligible=False, reason="plan_base_unresolved")
        return
    if receipt.get("tree_stable") is not True or not isinstance(tree, str):
        stamp.update(eligible=False, reason="plan_tree_unavailable")
        return

    try:
        fields, _, entries = verify_required.plan_snapshot(
            target, base, tree, deadline=monotonic() + verify_required.PLANNER_TIMEOUT_SECONDS
        )
        mode = fields["mode"]
        if mode == "unconfigured":
            plan.update(mode="unconfigured", reason="A required-test planner is not configured at the base.")
        elif (
            mode == "full"
            or len(fields["tests"]) > _MAX_LOCAL_TEST_FILES
            or (fields.get("test_files_total") == len(fields["tests"]))
        ):
            plan.update(
                mode="ci_required",
                required_files=fields["tests"],
                reason="Required CI on the head commit is the check for this change.",
            )
        else:
            required = fields["tests"]
            if any(_coverage_path(path) != path for path in required):
                raise ValueError("planner selected paths that cannot be used as whole test files")
            missing = sorted(set(required) - _covered_files(receipt, policy, entries))
            plan.update(
                mode="selective",
                required_files=required,
                missing_files=missing,
                run_argv=[*policy.argv_prefix, *required],
            )
            if missing:
                stamp.update(eligible=False, reason="plan_files_missing")
    except (OSError, RuntimeError, ValueError) as exc:
        reason = (exc.strerror if isinstance(exc, OSError) else str(exc)) or type(exc).__name__
        plan.update(mode="error", reason=" ".join(reason.split())[:200])
        stamp.update(eligible=False, reason="plan_error")


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


def reuse_gating(target: Path, receipt: Mapping[str, Any], *, current_tree: str | None = None) -> dict[str, Any] | None:
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
    if policy.needs_plan_coverage and current_tree is None:
        current_tree = localio.tree_fingerprint(target)
    if (
        isinstance(stamp, dict)
        and reason not in {"invalid_gating", "plan_error"}
        and policy.matches(stamp, current_tree)
    ):
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
    inherited = receipt.get("inherited_test_env")
    if not policy.needs_plan_coverage and isinstance(inherited, dict):
        # Preserve the legacy receipt shape when plan coverage is disabled.
        inherited.pop("py_set", None)
    if problem:
        eligible, reason = False, problem
    elif policy.mode == "unresolved":
        eligible, reason = False, policy.reason
    elif receipt.get("verify_manifest_id") and not policy.needs_plan_coverage:
        eligible, reason = True, "manifest_run"
    elif policy.mode == "audit_only":
        eligible, reason = True, "audit_only"
    else:
        eligible, reason = _required_check_verdict(receipt, policy)
    receipt["gating"] = stamp = policy.stamp(eligible, reason)
    if policy.needs_plan_coverage:
        _stamp_plan(target, receipt, policy, stamp)


_INELIGIBLE_NOTICE = {
    "not_completed": "The last run did not complete successfully.",
    "tree_changed_during_run": "The tree changed during the last run.",
    "not_the_required_check": "The last run did not run the required check.",
    "no_selector": "The last run had no test selector.",
    "no_tests_passed": "The last run passed no tests or reported test failures or errors.",
    "missing_gating": "The last run has no required-check verdict.",
    "invalid_gating": "The last run has an invalid required-check verdict.",
    "policy_changed": "The required check rule changed since the run.",
    "plan_base_unresolved": "The required-test plan needs a resolved Git base.",
    "plan_tree_unavailable": "The required-test plan needs a stable verified tree.",
}


def evaluate_receipt(
    policy: _GatePolicy, receipt: Mapping[str, Any], tree: str | None, *, deadline: float | None = None
) -> tuple[bool, str]:
    """Evaluate a saved receipt against a loaded policy and supplied tree without I/O."""
    if deadline is not None and monotonic() >= deadline:
        return False, "policy_timeout"
    if policy.mode == "unresolved":
        return False, policy.reason
    if policy.mode == "audit_only":
        problem = _blocked_reason(receipt)
        return problem is None, problem or "audit_only"
    if problem := _blocked_reason(receipt):
        return False, problem
    if policy.needs_plan_coverage and policy.fallback_source is not None:
        return False, "plan_base_unresolved"
    eligible, reason = receipt_is_gating(receipt)
    stamp = receipt.get("gating")
    if isinstance(stamp, dict) and reason != "invalid_gating":
        if not policy.matches(stamp, tree):
            return False, "policy_changed"
    return eligible, reason


def _suggested_argv(policy: _GatePolicy) -> list[str]:
    if policy.mode != "required_check":
        return ["pytest", "tests/test_example.py"]
    return [*policy.argv_prefix, *(["tests/test_example.py"] if policy.needs_arguments else [])]


def _policy_error(policy: _GatePolicy) -> str:
    detail = policy.error or "Git timed out reading the required check"
    return f"Could not load {_DECLARATION_PATH}: {detail}."


def _command_span(command: str) -> str:
    longest = max((len(run) for run in re.findall(r"`+", command)), default=0)
    delimiter = "`" * (longest + 1)
    contents = f" {command} " if longest else command
    return f"{delimiter}{contents}{delimiter}"


def _plan_guidance(receipt: Mapping[str, Any], reason: str) -> tuple[str | None, list[str] | None]:
    stamp = receipt.get("gating")
    plan = stamp.get("plan") if isinstance(stamp, dict) else None
    if isinstance(plan, dict):
        if reason == "plan_error":
            detail = plan.get("reason") or "unknown planner error"
            return f"Required-test planning failed ({detail}). Running verify again plans again.", None
        if reason == "plan_files_missing":
            missing, argv = plan.get("missing_files"), plan.get("run_argv")
            if (
                isinstance(missing, list)
                and isinstance(argv, list)
                and all(isinstance(item, str) for item in (*missing, *argv))
            ):
                shown = ", ".join(missing[:10])
                rest = f" and {len(missing) - 10} more" if len(missing) > 10 else ""
                return (
                    f"The last run missed required test files {shown}{rest}. {verify_required.PYTEST_OPTION_GUIDANCE}",
                    argv,
                )
    return _INELIGIBLE_NOTICE.get(reason), None


def _render_verify_command(target: Path, argv: list[str]) -> str:
    return f"brigade work verify run --target {shlex.quote(str(target))} --argv-json {shlex.quote(json.dumps(argv))}"


def receipt_counts_for_policy(
    target: Path, receipt: Mapping[str, Any], *, current_tree: str | None = None
) -> tuple[bool, str | None]:
    """Check a receipt against the current policy and explain a rejection with rerun guidance."""
    policy = _load_policy(target, deadline=monotonic() + _CLOSEOUT_POLICY_BUDGET_SECONDS)
    if policy.needs_plan_coverage and current_tree is None:
        current_tree = localio.tree_fingerprint(target)
    eligible, reason = evaluate_receipt(policy, receipt, current_tree)
    if eligible:
        return True, None
    if reason == "policy_timeout" and policy.mode == "unresolved":
        return False, "Git timed out while reading the required check, so run closeout again."
    if policy.mode == "unresolved":
        return False, f"The repository's required check could not be read. {_policy_error(policy)}"
    explanation, argv = _plan_guidance(receipt, reason)
    explanation = explanation or "The last run has an ineligible required-check verdict."
    if reason == "plan_base_unresolved" and policy.base_message:
        explanation += f" {policy.base_message}"
    if policy.mode == "audit_only" or reason in {"plan_error", "plan_base_unresolved"}:
        return False, explanation
    command = _render_verify_command(target, argv or _suggested_argv(policy))
    return (
        False,
        "The latest verification run for this session is not the repository's required check. "
        f"{explanation} Run {_command_span(command)} in a POSIX shell.",
    )


class StopGate:
    """Share one lazy policy lookup across receipt checks and Stop guidance."""

    def __init__(self, target: Path, *, deadline: float | None = None) -> None:
        self.target = target
        self.deadline = deadline
        self._last_ineligible: tuple[datetime, datetime, str, Mapping[str, Any]] | None = None

    @cached_property
    def _policy(self) -> _GatePolicy:
        return _load_policy(self.target, deadline=self.deadline)

    @property
    def timed_out(self) -> bool:
        return self._policy.reason == "policy_timeout"

    @property
    def requires_plan(self) -> bool:
        return self._policy.needs_plan_coverage

    @cached_property
    def current_tree(self) -> str | None:
        return localio.tree_fingerprint(self.target)

    def _times_in_window(
        self, receipt: Mapping[str, Any], started: datetime, fingerprint: str | None
    ) -> tuple[datetime, datetime] | None:
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
        return receipt_started, completed

    def completion_in_window(
        self, receipt: Mapping[str, Any], started: datetime, fingerprint: str | None
    ) -> datetime | None:
        times = self._times_in_window(receipt, started, fingerprint)
        if times is None:
            return None
        receipt_started, completed = times
        current_tree = self.current_tree if self.requires_plan else None
        eligible, reason = evaluate_receipt(self._policy, receipt, current_tree)
        if eligible:
            return completed
        stamp = receipt.get("gating")
        if self.requires_plan or (isinstance(stamp, dict) and stamp.get("eligible") is False):
            if self._last_ineligible is None or completed > self._last_ineligible[0]:
                self._last_ineligible = completed, receipt_started, reason, receipt
        return None

    def latest_completion(
        self, receipts: Iterable[dict[str, Any]], started: datetime, fingerprint: str | None
    ) -> Iterator[tuple[dict[str, Any], datetime]]:
        """Use the newest applicable receipt for an opted-in policy, as closeout does."""
        tree = self.current_tree
        if tree is None:
            return
        latest: tuple[datetime, dict[str, Any]] | None = None
        for receipt in receipts:
            if receipt.get("status") in {"busy", "canceled"} or receipt.get("tree_fingerprint") != tree:
                continue
            times = self._times_in_window(receipt, started, fingerprint)
            if times is not None and (latest is None or times[0] > latest[0]):
                latest = times[0], receipt
        if latest is not None:
            receipt = latest[1]
            if (completed := self.completion_in_window(receipt, started, fingerprint)) is not None:
                yield receipt, completed

    def command(self) -> list[str]:
        if self._last_ineligible:
            _, _, reason, receipt = self._last_ineligible
            _, argv = _plan_guidance(receipt, reason)
            if argv:
                return argv
        return _suggested_argv(self._policy)

    def notice(self) -> str | None:
        policy = self._policy
        if self.timed_out and policy.base_message is None:
            return f"{self.target}: Git timed out reading the required check. Stop skipped this check."
        if policy.fallback_source is None and policy.base_message is None:
            return None
        source = {
            "default_branch_tip": "Using the declaration from the fetched default branch tip.",
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
            message = f"{self.target}: {_policy_error(policy)}"
        else:
            message = f"{self.target}: run {_command_span(replacement)} in a POSIX shell."
            if policy.mode == "audit_only":
                message += "\n  This repository declares no required check."
        threshold = localio.parse_iso_datetime(since)
        if self._last_ineligible and threshold is not None and self._last_ineligible[1] >= threshold:
            _, _, reason, receipt = self._last_ineligible
            explanation, _ = _plan_guidance(receipt, reason)
            if reason in {"plan_error", "plan_base_unresolved"}:
                return f"{self.target}: {explanation}"
            if explanation:
                message += f"\n  {explanation}"
        return message
