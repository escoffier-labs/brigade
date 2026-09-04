"""Signed requester evidence created before Brigade dispatches a run."""

from __future__ import annotations

import hashlib
import re
import secrets
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from . import attestation, localio, run_checkpoint, run_events, run_journal, run_lifecycle, runguard

AGENT_REQUEST_PREDICATE_TYPE = "https://brigade.dev/attestation/agent-request/v1"
_HEX40_RE = re.compile(r"^[0-9a-f]{40}$")


class AgentRequestError(RuntimeError):
    """Requester evidence is absent, unsafe, or cannot be signed."""


def task_digest(task: str) -> str:
    return hashlib.sha256(task.encode("utf-8")).hexdigest()


def _timestamp(value: datetime) -> str:
    return run_events.format_recorded_at(value.astimezone(timezone.utc))


def _baseline_commit(target: Path) -> str:
    try:
        result = subprocess.run(
            ["git", "-C", str(target), "rev-parse", "HEAD"],
            check=False,
            capture_output=True,
            text=True,
            stdin=subprocess.DEVNULL,
            timeout=30,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise AgentRequestError("request signing could not read the Git HEAD commit") from exc
    value = result.stdout.strip()
    if result.returncode != 0 or not _HEX40_RE.fullmatch(value):
        raise AgentRequestError("request signing requires a readable Git HEAD commit")
    return value


def build_statement(*, run_id: str, baseline_commit: str, task: str, requested_at: str, nonce: str) -> dict[str, Any]:
    """Build a public-safe request statement without embedding the task text."""
    return {
        "_type": attestation.IN_TOTO_STATEMENT_TYPE,
        "subject": [{"name": "git:baseline", "digest": {"gitCommit": baseline_commit}}],
        "predicateType": AGENT_REQUEST_PREDICATE_TYPE,
        "predicate": {
            "schemaVersion": 1,
            "run": {"id": run_id},
            "taskSha256": task_digest(task),
            "requestedAt": requested_at,
            "nonce": nonce,
        },
    }


def event_payload(
    *,
    requester_principal: str,
    requester_keyid: str,
    baseline_commit: str,
    task: str,
    nonce: str,
    statement_sha256: str,
    attestation_path: str,
) -> dict[str, str]:
    return {
        "requester_principal": requester_principal,
        "requester_keyid": requester_keyid,
        "baseline_commit": baseline_commit,
        "task_sha256": task_digest(task),
        "nonce": nonce,
        "statement_sha256": statement_sha256,
        "attestation_path": attestation_path,
    }


def _safe_key_path(key: Path) -> Path:
    try:
        expanded = key.expanduser()
        is_symlink = expanded.is_symlink()
        resolved = expanded.resolve()
        is_file = resolved.is_file()
    except (OSError, RuntimeError) as exc:
        raise AgentRequestError("requester key must be a regular private key file") from exc
    if is_symlink or not is_file:
        raise AgentRequestError("requester key must be a regular private key file")
    return resolved


def record_request(
    *, target: Path, run_dir: Path, task: str, key: Path, principal: str | None = None
) -> dict[str, str]:
    """Sign and append the request evidence before any worker dispatches."""
    try:
        target = target.expanduser().resolve()
        run_dir = run_dir.expanduser().resolve()
        key_path = _safe_key_path(key)
        run_meta = localio.read_json_dict(run_dir / "run.json")
        if run_meta is None:
            raise AgentRequestError("run.json is unavailable for requester signing")
        workspace = runguard.resolve_run_lock_workspace(run_meta, run_dir)
        if workspace is None or not runguard.is_active_run_owner(workspace, run_dir):
            raise AgentRequestError("request signing requires the active run lock")
        run_lifecycle.prepare_lifecycle_journal(run_dir, workspace=workspace, incoming_snapshot=run_meta)
        report = run_journal.read_journal_bounded(run_dir / "events" / "lifecycle.jsonl")
        if report.partial_tail is not None or report.chain_errors:
            raise AgentRequestError("lifecycle journal is unavailable for requester signing")
        if any(event.event_type == "request.signed" for event in report.events):
            raise AgentRequestError("a signed request is already recorded for this run")

        run_id = run_dir.name
        baseline_commit = _baseline_commit(target)
        requested_at = _timestamp(datetime.now(timezone.utc))
        nonce = secrets.token_hex(16)
        statement = build_statement(
            run_id=run_id, baseline_commit=baseline_commit, task=task, requested_at=requested_at, nonce=nonce
        )
        envelope = attestation.create_envelope(statement, key_path)
        signature = attestation.verify_attestation(
            envelope,
            allowed_signers_path=attestation.default_allowed_signers_path(target),
            principal=principal,
            target=target,
            expected_predicate_type=AGENT_REQUEST_PREDICATE_TYPE,
        )
        if signature.status != attestation.STATUS_SIGNED_OK or not signature.principal or not signature.keyid:
            raise AgentRequestError("requester signing key and principal are not trusted by allowed_signers")
        relative_path = Path("requests") / f"{nonce}.json"
        statement_sha256 = hashlib.sha256(attestation.canonical_statement_bytes(statement)).hexdigest()
        payload = event_payload(
            requester_principal=signature.principal,
            requester_keyid=signature.keyid,
            baseline_commit=baseline_commit,
            task=task,
            nonce=nonce,
            statement_sha256=statement_sha256,
            attestation_path=relative_path.as_posix(),
        )
        attestation.write_attestation_file(envelope, run_dir / relative_path)
        run_lifecycle.record_lifecycle_event(
            run_dir,
            event_type="request.signed",
            payload=payload,
            idempotency_key=f"request:{nonce}",
            workspace=workspace,
        )

        # Use the sanctioned run.json writer so the request projection receives
        # its own recovery checkpoint and authority/shadow parity checks.
        from . import aboyeur

        aboyeur.update_run_receipt(
            run_dir,
            requester_principal=signature.principal,
            requester_keyid=signature.keyid,
            request=payload,
        )
        return payload
    except AgentRequestError:
        raise
    except (
        attestation.AttestationError,
        run_checkpoint.CheckpointError,
        run_journal.RunJournalError,
        run_lifecycle.LifecycleJournalError,
        OSError,
    ) as exc:
        detail = " ".join(str(exc).split())
        raise AgentRequestError(f"request recording failed: {detail or type(exc).__name__}") from exc
