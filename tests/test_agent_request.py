"""Signed agent-request statements are public-safe run-creation evidence."""

from __future__ import annotations

import base64
import json
import shutil
from pathlib import Path

import pytest

from brigade import agent_request, attestation, run_events


if not shutil.which("ssh-keygen"):
    pytest.skip("ssh-keygen is required for agent request tests", allow_module_level=True)


def test_request_statement_binds_only_digests_and_identity(tmp_path: Path) -> None:
    key_path, signers = attestation.keygen(tmp_path, principal="requester")
    statement = agent_request.build_statement(
        run_id="run-001",
        baseline_commit="a" * 40,
        task="task text must not be public",
        requested_at="2026-09-04T12:00:00.000000Z",
        nonce="b" * 32,
    )
    envelope = attestation.create_envelope(statement, key_path)
    decoded = json.loads(base64.b64decode(envelope["payload"]))

    assert decoded["predicateType"] == agent_request.AGENT_REQUEST_PREDICATE_TYPE
    assert decoded["predicate"]["taskSha256"] == agent_request.task_digest("task text must not be public")
    assert "task text must not be public" not in json.dumps(decoded)
    assert (
        attestation.verify_attestation(
            envelope,
            allowed_signers_path=signers,
            expected_predicate_type=agent_request.AGENT_REQUEST_PREDICATE_TYPE,
        ).status
        == attestation.STATUS_SIGNED_OK
    )


def test_request_event_carries_statement_digest_not_task_text() -> None:
    payload = agent_request.event_payload(
        requester_principal="requester",
        requester_keyid="SHA256:key",
        baseline_commit="a" * 40,
        task="private task text",
        nonce="b" * 32,
        statement_sha256="c" * 64,
        attestation_path="requests/bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb.json",
    )

    run_events.validate_event_request(
        event_type="request.signed",
        payload=payload,
        idempotency_key="request:bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",
    )
    assert "private task text" not in json.dumps(payload)
