"""Signed agent-request statements are public-safe run-creation evidence."""

from __future__ import annotations

import base64
import json
import shutil
import subprocess
from pathlib import Path

import pytest

from brigade import aboyeur, agent_request, attestation, run_events, run_journal, runguard
from brigade.roster import Agent, Roster


if not shutil.which("ssh-keygen"):
    pytest.skip("ssh-keygen is required for agent request tests", allow_module_level=True)


def _request_run(tmp_path: Path, *, principal: str = "requester") -> tuple[Path, Path, Path]:
    target = tmp_path / "repo"
    target.mkdir()
    subprocess.run(["git", "init", "-q", str(target)], check=True, stdin=subprocess.DEVNULL)
    subprocess.run(
        ["git", "-C", str(target), "config", "user.email", "test@example.invalid"],
        check=True,
        stdin=subprocess.DEVNULL,
    )
    subprocess.run(
        ["git", "-C", str(target), "config", "user.name", "Test User"],
        check=True,
        stdin=subprocess.DEVNULL,
    )
    (target / "tracked.txt").write_text("base\n")
    subprocess.run(["git", "-C", str(target), "add", "tracked.txt"], check=True, stdin=subprocess.DEVNULL)
    subprocess.run(
        ["git", "-C", str(target), "commit", "-q", "-m", "initial"],
        check=True,
        stdin=subprocess.DEVNULL,
    )
    key_path, _ = attestation.keygen(target, principal=principal)
    run_dir = target / ".brigade" / "runs" / "request-run-001"
    roster = Roster(orchestrator="chef", agents={"chef": Agent("chef", "codex", "plan")})
    aboyeur.record_run_start(
        run_dir,
        task="private task",
        cwd=target,
        roster=roster,
        read_only=True,
        lock_workspace=target,
    )
    return target, run_dir, key_path


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


def test_record_request_pairs_lifecycle_event_and_projects_verified_identity(tmp_path: Path) -> None:
    target, run_dir, key_path = _request_run(tmp_path)

    with runguard.run_lock(target, run_dir=run_dir):
        payload = agent_request.record_request(
            target=target,
            run_dir=run_dir,
            task="private task",
            key=key_path,
            principal="requester",
        )

    report = run_journal.read_journal_bounded(run_dir / "events" / "lifecycle.jsonl")
    request_index = next(index for index, event in enumerate(report.events) if event.event_type == "request.signed")
    checkpoint = report.events[request_index - 1]
    assert checkpoint.event_type == "run.snapshot.checkpointed"
    assert checkpoint.payload["paired_event_type"] == "request.signed"
    assert report.partial_tail is None
    assert report.chain_errors == []

    receipt = json.loads((run_dir / "run.json").read_text())
    verified = attestation.verify_attestation(
        json.loads((run_dir / payload["attestation_path"]).read_text()),
        allowed_signers_path=attestation.default_allowed_signers_path(target),
        principal="requester",
        target=target,
        expected_predicate_type=agent_request.AGENT_REQUEST_PREDICATE_TYPE,
    )
    assert receipt["request"] == payload
    assert receipt["requester_principal"] == verified.principal == "requester"
    assert receipt["requester_keyid"] == payload["requester_keyid"] == verified.keyid
    assert payload["statement_sha256"] == report.events[request_index].payload["statement_sha256"]


def test_record_request_rejects_duplicate_without_second_event(tmp_path: Path) -> None:
    target, run_dir, key_path = _request_run(tmp_path)

    with runguard.run_lock(target, run_dir=run_dir):
        agent_request.record_request(target=target, run_dir=run_dir, task="private task", key=key_path)
        with pytest.raises(agent_request.AgentRequestError, match="already recorded"):
            agent_request.record_request(target=target, run_dir=run_dir, task="private task", key=key_path)

    events = run_journal.read_journal_bounded(run_dir / "events" / "lifecycle.jsonl").events
    assert sum(event.event_type == "request.signed" for event in events) == 1


def test_record_request_applies_target_revoked_keys(tmp_path: Path) -> None:
    target, run_dir, key_path = _request_run(tmp_path)
    krl_path = attestation.default_revoked_keys_path(target)
    subprocess.run(
        ["ssh-keygen", "-k", "-f", str(krl_path), str(key_path.with_suffix(".pub"))],
        check=True,
        capture_output=True,
        stdin=subprocess.DEVNULL,
    )

    with runguard.run_lock(target, run_dir=run_dir):
        with pytest.raises(agent_request.AgentRequestError, match="not trusted"):
            agent_request.record_request(target=target, run_dir=run_dir, task="private task", key=key_path)

    journal = run_dir / "events" / "lifecycle.jsonl"
    events = run_journal.read_journal_bounded(journal).events
    assert all(event.event_type != "request.signed" for event in events)
    receipt = json.loads((run_dir / "run.json").read_text())
    assert "request" not in receipt
    assert list((run_dir / "requests").glob("*.json")) == []


def test_record_request_normalizes_projection_failure_without_lone_event(tmp_path: Path, monkeypatch) -> None:
    target, run_dir, key_path = _request_run(tmp_path)

    monkeypatch.setattr(aboyeur, "update_run_receipt", lambda *args, **kwargs: (_ for _ in ()).throw(OSError("full")))
    with runguard.run_lock(target, run_dir=run_dir):
        with pytest.raises(agent_request.AgentRequestError, match="request recording failed: full"):
            agent_request.record_request(target=target, run_dir=run_dir, task="private task", key=key_path)

    events = run_journal.read_journal_bounded(run_dir / "events" / "lifecycle.jsonl").events
    request_index = next(index for index, event in enumerate(events) if event.event_type == "request.signed")
    assert request_index > 0
    assert events[request_index - 1].event_type == "run.snapshot.checkpointed"
    assert events[request_index - 1].payload["paired_event_type"] == "request.signed"


def test_safe_key_path_rejects_symlink_after_tilde_expansion(tmp_path: Path, monkeypatch) -> None:
    home = tmp_path / "home"
    home.mkdir()
    private_key = home / "private-key"
    private_key.write_text("private")
    (home / "linked-key").symlink_to(private_key)
    monkeypatch.setenv("HOME", str(home))

    with pytest.raises(agent_request.AgentRequestError, match="regular private key"):
        agent_request._safe_key_path(Path("~/linked-key"))
