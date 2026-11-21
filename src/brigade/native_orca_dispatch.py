"""Caller-mediated local Orca worker-start contract."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Mapping, TypeGuard
from uuid import UUID

from . import run_task_journal
from .native_child_host import NativeChildError
from .native_child_journal import digest
from .native_orca_host import (
    HOST,
    NEW_WORKTREES,
    ORCA_VERSION,
    SUPPORTED_VERSIONS,
    NativeTarget,
    require_supported_target,
)

_START_DOMAIN = "brigade.native-orca.worker-start.v1"
_HANDLE_DOMAIN = "brigade.native-orca.handle.v1"
_HEX_ID = re.compile(r"[0-9a-f]{64}", re.ASCII)
_NATIVE_ID = re.compile(r"(run|task|ctx|msg)_[0-9a-f]{12}", re.ASCII)

CLI = ("orca-ide", "orchestration")


def valid_id(value: object, prefix: str) -> TypeGuard[str]:
    return isinstance(value, str) and value.startswith(prefix + "_") and _NATIVE_ID.fullmatch(value) is not None


def request_key(run_id: str, task_id: str, attempt_id: str) -> str:
    if (
        not run_id.startswith(HOST.run_prefix)
        or not _HEX_ID.fullmatch(run_id.removeprefix(HOST.run_prefix))
        or not run_task_journal.task_id(task_id)
        or not _HEX_ID.fullmatch(attempt_id)
    ):
        raise NativeChildError("invalid-identity", "the Brigade claim identity is invalid")
    return mutation_key({"run": run_id, "task": task_id, "attempt": attempt_id}, _START_DOMAIN)


def mutation_key(material: Mapping[str, object], domain: str) -> str:
    # The Orca CLI and SSH relay both require UUID-shaped retry IDs.
    key = bytearray.fromhex(digest(material, domain)[:32])
    key[6] = (key[6] & 0x0F) | 0x80
    key[8] = (key[8] & 0x3F) | 0x80
    return str(UUID(bytes=bytes(key)))


@dataclass(frozen=True)
class LaunchBinding:
    brigade_run_id: str
    brigade_task_id: str
    brigade_attempt_id: str
    run_id: str
    task_id: str
    retry_key: str | None

    def checked_key(self) -> str:
        expected = request_key(self.brigade_run_id, self.brigade_task_id, self.brigade_attempt_id)
        if self.retry_key != expected:
            raise NativeChildError("retry-request-invalid", "the retained Orca retry request is missing or invalid")
        if not valid_id(self.run_id, "run") or not valid_id(self.task_id, "task"):
            raise NativeChildError("invalid-identity", "the retained Orca Run or Task identity is invalid")
        return expected


@dataclass(frozen=True)
class WorkerReference:
    """Identity only. All worker text and machine-local details stay in the caller's inputs."""

    binding: LaunchBinding
    dispatch_id: str
    orca_version: str = ORCA_VERSION

    def __post_init__(self) -> None:
        self.binding.checked_key()
        if self.orca_version not in SUPPORTED_VERSIONS or not valid_id(self.dispatch_id, "ctx"):
            raise NativeChildError("native-invalid", "the Orca worker reference is invalid")

    @property
    def run_id(self) -> str:
        return self.binding.run_id

    @property
    def task_id(self) -> str:
        return self.binding.task_id

    @property
    def handle_digest(self) -> str:
        return digest(
            {"request": self.binding.retry_key, "run": self.run_id, "task": self.task_id, "dispatch": self.dispatch_id},
            _HANDLE_DOMAIN,
        )


def launch_argv(binding: LaunchBinding, target: NativeTarget) -> tuple[str, ...]:
    """Describe a local start for the caller's existing Run and Task.

    New worktrees inherit the local coordinator's repository on the Orca server.
    A caller must bind the claim to that coordinator before handing out argv.
    """
    key = binding.checked_key()
    require_supported_target(target)
    argv = [
        *CLI,
        "worker-start",
        "--environment",
        "local",
        "--retry-request",
        key,
        "--run",
        binding.run_id,
        "--task",
        binding.task_id,
        "--worktree",
        target.worktree,
    ]
    if target.worktree in NEW_WORKTREES:
        argv.extend(("--base-branch", target.source_revision))
    argv.extend(("--agent", target.agent))
    if target.launch_model is not None:
        argv.extend(("--model", target.launch_model))
    if target.effort is not None:
        argv.extend(("--effort", target.effort))
    # The concise view drops mutation.requestId and may exit zero for an unknown start.
    return (*argv, "--json", "--full")


def result_fields(payload: object, *, ok: bool = True) -> Mapping[str, object]:
    """Read an Orca JSON result without carrying error messages forward."""
    if not isinstance(payload, Mapping) or (ok and payload.get("ok") is not True):
        return {}
    result = payload.get("result")
    return result if isinstance(result, Mapping) else {}


# Orca v1.4.269 src/main/runtime/orchestration/db/tasks/task-start-claim.ts
# rejects any non-ready Task, as does the AppImage's compiled
# orcad-retained-source-BfK6WWlH.js. Only failed qualifies for a direct
# refusal. worker-dispatch-settlement.ts separates Task settlement from
# process exit, so a Task refusal cannot settle a previously ambiguous launch.
_TASK_STATES_FOR_REFUSAL = frozenset({"failed"})


class DefiniteStartRefusal(NativeChildError):
    """A matching refusal that produced no live worker for this claim."""


def parse_start(binding: LaunchBinding, target: NativeTarget, payload: object, *, exit_code: int) -> WorkerReference:
    """Correlate a ready, replay-safe worker-start receipt, with no retry advice."""
    binding.checked_key()
    require_supported_target(target)
    reply = result_fields(payload, ok=False)
    error = payload.get("error") if isinstance(payload, Mapping) else None
    if (
        exit_code != 0
        and isinstance(payload, Mapping)
        and payload.get("ok") is False
        and isinstance(error, Mapping)
        and error.get("code") == "task_not_startable"
        and not reply
    ):
        data = error.get("data")
        data = data if isinstance(data, Mapping) else {}
        status = data.get("status")
        if not isinstance(status, str) or status not in _TASK_STATES_FOR_REFUSAL:
            raise NativeChildError("start-unknown", "the Orca Task refusal does not establish a failed start")
        # task-dispatch-refusal.ts always emits taskId, status and unmetDependencies.
        # The refusal contract adds nextSteps and omits runId in these errors.
        if data.get("taskId") != binding.task_id or data.get("runId", binding.run_id) != binding.run_id:
            raise NativeChildError("native-mismatch", "the Orca start refusal does not match the retained claim")
        if valid_id(data.get("retryOf"), "ctx") and type(data.get("failureCount")) is int and data["failureCount"] >= 3:
            raise DefiniteStartRefusal(
                "circuit-broken", "Orca refused another start in the retained Task's retry chain"
            )
        raise DefiniteStartRefusal("start-refused", "Orca refused to start the retained Task")
    if exit_code != 0 and reply.get("state") == "failed" and result_fields(payload):
        _start_reference(binding, target, reply)
        # Orca can report failed and still retain a session from that start.
        if reply.get("residualResources") == [] and reply.get("recovery") is None:
            raise DefiniteStartRefusal("start-failed", "Orca reported a failed worker start")
    if exit_code != 0 or reply.get("state") != "ready" or not result_fields(payload):
        raise NativeChildError("start-unknown", "the Orca worker start was not confirmed")
    return _start_reference(binding, target, reply)


def unconfirmed_start(binding: LaunchBinding, target: NativeTarget, payload: object) -> WorkerReference:
    """Recover identity from the original unknown receipt, never from a notice."""
    reply = result_fields(payload)
    if reply.get("state") != "outcome_unknown":
        raise NativeChildError("start-unknown", "a full outcome_unknown start receipt is required for recovery")
    return _start_reference(binding, target, reply)


def _start_reference(binding: LaunchBinding, target: NativeTarget, reply: Mapping[str, object]) -> WorkerReference:
    key = binding.checked_key()
    # Full worker-start receipts always carry runId, taskId and dispatchId.
    # The mutation wrapper supplies requestId and replayed.
    dispatch_id = reply.get("dispatchId")
    mutation = reply.get("mutation")
    launch = reply.get("launch")
    effective = launch.get("effective") if isinstance(launch, Mapping) else None
    if (
        reply.get("runId") != binding.run_id
        or reply.get("server") is not None
        or reply.get("coordinatedFrom") is not None
        or reply.get("taskId") != binding.task_id
        or not valid_id(dispatch_id, "ctx")
        or not isinstance(mutation, Mapping)
        or mutation.get("requestId") != key
        or type(mutation.get("replayed")) is not bool
        or not isinstance(effective, Mapping)
        or effective.get("agent") != target.agent
        or (target.launch_model is not None and effective.get("model") != target.launch_model)
        or (target.effort is not None and effective.get("effort") != target.effort)
    ):
        raise NativeChildError("native-mismatch", "the Orca start receipt does not match the retained claim")
    return WorkerReference(binding, dispatch_id, target.orca_version)
