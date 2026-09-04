"""Signed requester evidence created before Brigade dispatches a run."""

from __future__ import annotations

import hashlib
import re
import secrets
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from . import attestation, localio, run_events, run_journal, run_projector

AGENT_REQUEST_PREDICATE_TYPE = "https://brigade.dev/attestation/agent-request/v1"
_HEX40_RE = re.compile(r"^[0-9a-f]{40}$")


class AgentRequestError(RuntimeError):
    """Requester evidence is absent, unsafe, or cannot be signed."""


def task_digest(task: str) -> str:
    return hashlib.sha256(task.encode("utf-8")).hexdigest()


def _timestamp(value: datetime) -> str:
    return run_events.format_recorded_at(value.astimezone(timezone.utc))


def _baseline_commit(target: Path) -> str:
    result = subprocess.run(
        ["git", "-C", str(target), "rev-parse", "HEAD"],
        check=False,
        capture_output=True,
        text=True,
        timeout=30,
    )
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
    resolved = key.expanduser().resolve()
    if key.is_symlink() or not resolved.is_file():
        raise AgentRequestError("requester key must be a regular private key file")
    return resolved


def record_request(
    *, target: Path, run_dir: Path, task: str, key: Path, principal: str | None = None
) -> dict[str, str]:
    """Sign and append the request evidence before any worker dispatches."""
    target = target.expanduser().resolve()
    run_dir = run_dir.expanduser().resolve()
    key_path = _safe_key_path(key)
    run_meta = localio.read_json_dict(run_dir / "run.json")
    if run_meta is None:
        raise AgentRequestError("run.json is unavailable for requester signing")
    report = run_journal.read_journal_bounded(run_dir / "events" / "lifecycle.jsonl")
    if report.partial_tail is not None or report.chain_errors or not report.events:
        raise AgentRequestError("lifecycle journal is unavailable for requester signing")
    run_id = run_dir.name
    baseline_commit = _baseline_commit(target)
    requested_at = _timestamp(datetime.now(timezone.utc))
    nonce = secrets.token_hex(16)
    statement = build_statement(
        run_id=run_id, baseline_commit=baseline_commit, task=task, requested_at=requested_at, nonce=nonce
    )
    keyid = attestation.get_key_fingerprint(key_path)
    envelope = attestation.create_envelope(statement, key_path)
    signature = attestation.verify_attestation(
        envelope,
        allowed_signers_path=attestation.default_allowed_signers_path(target),
        principal=principal,
        expected_predicate_type=AGENT_REQUEST_PREDICATE_TYPE,
    )
    if signature.status != attestation.STATUS_SIGNED_OK or not signature.principal:
        raise AgentRequestError("requester signing key and principal are not trusted by allowed_signers")
    relative_path = Path("requests") / f"{nonce}.json"
    statement_sha256 = hashlib.sha256(attestation.canonical_statement_bytes(statement)).hexdigest()
    attestation.write_attestation_file(envelope, run_dir / relative_path)
    payload = event_payload(
        requester_principal=signature.principal,
        requester_keyid=keyid,
        baseline_commit=baseline_commit,
        task=task,
        nonce=nonce,
        statement_sha256=statement_sha256,
        attestation_path=relative_path.as_posix(),
    )
    run_journal.append_event(
        run_dir / "events" / "lifecycle.jsonl",
        run_id=run_id,
        event_type="request.signed",
        payload=payload,
        idempotency_key=f"request:{nonce}",
        expected_previous_sequence=report.events[-1].sequence,
        recorded_at=requested_at,
    )
    updated = dict(run_meta)
    updated.update({"requester_principal": signature.principal, "requester_keyid": keyid, "request": payload})
    after = run_journal.read_journal_bounded(run_dir / "events" / "lifecycle.jsonl")
    projection = run_projector.project_run_snapshot(updated, after.events, journal_present=True)
    localio.write_bytes_atomic(run_dir / "run.json", projection.to_bytes())
    return payload
