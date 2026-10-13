"""Message envelope receipts stay metadata-only and never replay legacy bodies."""

from __future__ import annotations

import json
from pathlib import Path
from typing import cast

from brigade import agents, message_envelope, proc, provenance
from brigade.run_receipts import agent_result_payload, worker_payload, write_agent_logs, write_worker_logs
from brigade.run_transport import WorkerAttempt, WorkerResult


def test_message_receipt_omits_body_and_absolute_paths(tmp_path: Path):
    delivery = message_envelope.emit(
        "implementation output",
        kind="worker-result",
        producer="run_transport.dispatch",
        from_seat="coder",
        to_seat="chef",
        run_dir=tmp_path,
        run_id="demo-run",
        session_harness="codex",
    )
    path = tmp_path / "message-envelopes.jsonl"
    record = json.loads(path.read_text().splitlines()[0])
    assert set(record) >= {"message_id", "phase", "from_seat", "to_seat", "envelope"}
    assert "text" not in record
    assert "prompt" not in record
    assert "body" not in record
    assert message_envelope.receipt_has_message_body(record) is False
    locator = record["envelope"]["locator"]["value"]
    assert record["envelope"]["locator"]["kind"] == "repo-relative"
    assert not provenance.is_absolute_locator(locator)
    assert not locator.startswith("/")
    assert ".." not in locator.split("/")
    dumped = json.dumps(record)
    assert str(tmp_path) not in dumped
    assert delivery.envelope["trust"]["label"] == "untrusted"


def test_worker_payload_includes_envelope_without_new_body_fields():
    delivery = message_envelope.emit(
        "implementation output",
        kind="worker-result",
        producer="run_transport.dispatch",
        from_seat="coder",
        to_seat="chef",
        run_id="demo-run",
    )
    result = WorkerResult(
        worker="coder",
        task="implement it",
        text="implementation output",
        ok=True,
        provenance=delivery.envelope,
    )
    entry = worker_payload([result])[0]
    assert entry["text"] == "implementation output"
    assert entry["provenance"]["hashes"]["content_scope"] == "message.text.utf8.v1"
    assert entry["provenance"]["trust"]["label"] == "untrusted"
    assert "prompt" not in entry
    assert "message_body" not in entry


def test_worker_payload_includes_cloud_environment_fingerprint():
    fingerprint = {"environment_fingerprint": "sha256:abc123def456"}
    result = WorkerResult(
        worker="cloud-worker",
        task="fix it",
        text="done",
        ok=True,
        cloud_environment=fingerprint,
    )
    entry = worker_payload([result])[0]
    assert entry["cloud_environment"] == fingerprint
    assert "environment_id" not in entry["cloud_environment"]


def test_worker_payload_distinguishes_declared_and_observed_model_evidence():
    result = WorkerResult(
        worker="cursor-worker",
        task="fix it",
        text="done",
        ok=True,
        transport="acpx",
        requested_model="cursor-auto",
        effective_model="composer-2.5",
        acpx_version="0.12.0",
        metadata_provenance={
            "observed_at": "2026-10-13T12:00:00+00:00",
            "provider_error": "Bearer super-secret-token",
            "fields": {
                "requested_model": {
                    "source": "declared",
                    "locator": "launch-argument",
                    "digest": None,
                    "unknown_reason": "mutable-alias-not-backend-identity",
                },
                "effective_model": {
                    "source": "observed",
                    "locator": "acp-config-option-update",
                    "digest": None,
                    "unknown_reason": "backend-revision-unavailable",
                },
                "acpx_version": {
                    "source": "observed",
                    "locator": "acpx-version-output",
                    "digest": None,
                    "unknown_reason": None,
                },
            },
        },
    )

    entry = worker_payload([result])[0]

    assert entry["metadata_provenance"]["fields"]["requested_model"]["source"] == "declared"
    assert entry["metadata_provenance"]["fields"]["effective_model"]["source"] == "observed"
    assert entry["metadata_provenance"]["fields"]["effective_model"]["unknown_reason"] == (
        "backend-revision-unavailable"
    )
    assert "cursor-auto" not in str(entry["metadata_provenance"])
    assert "super-secret-token" not in str(entry)


def test_worker_payload_drops_non_mapping_metadata_provenance():
    result = WorkerResult(
        worker="cursor-worker",
        task="inspect metadata",
        text="done",
        ok=True,
        metadata_provenance=cast(dict[str, object], []),
    )

    entry = worker_payload([result])[0]

    assert "metadata_provenance" not in entry


def test_worker_payload_drops_invalid_metadata_provenance_blocks():
    timestamp = "2026-10-13T12:00:00+00:00"
    invalid_blocks = (
        {
            "observed_at": "2026-99-99T99:99:99+99:99",
            "fields": {
                "effective_model": {
                    "source": "observed",
                    "locator": "acpx-session-update",
                    "digest": None,
                    "unknown_reason": "backend-revision-unavailable",
                }
            },
        },
        {
            "observed_at": timestamp,
            "fields": {"effective_model": {"digest": None, "unknown_reason": None}},
        },
        {
            "observed_at": timestamp,
            "fields": {
                "effective_model": {
                    "source": "observed",
                    "locator": "launch-argument",
                    "digest": None,
                    "unknown_reason": "backend-revision-unavailable",
                }
            },
        },
        {
            "observed_at": timestamp,
            "fields": {
                "effective_model": {
                    "source": "observed",
                    "locator": "acp-config-option-update",
                    "digest": "a" * 64,
                    "unknown_reason": "backend-revision-unavailable",
                }
            },
        },
    )

    for metadata_provenance in invalid_blocks:
        result = WorkerResult(
            worker="cursor-worker",
            task="inspect metadata",
            text="done",
            ok=True,
            metadata_provenance=metadata_provenance,
        )
        entry = worker_payload([result])[0]

        assert "metadata_provenance" not in entry


def test_worker_payload_bounds_model_and_adapter_version_labels():
    credential_shaped_model = "ghp_FAKE0123456789abcdefghijklmnopqrstuvwxyz"
    metadata_provenance = {
        "observed_at": "2026-10-13T12:00:00+00:00",
        "fields": {
            "requested_model": {
                "source": "declared",
                "locator": "launch-argument",
                "digest": None,
                "unknown_reason": "launched-argument-not-backend-observation",
            },
            "effective_model": {
                "source": "observed",
                "locator": "acp-config-option-update",
                "digest": None,
                "unknown_reason": "backend-revision-unavailable",
            },
            "acpx_version": {
                "source": "observed",
                "locator": "acpx-version-output",
                "digest": None,
                "unknown_reason": None,
            },
        },
    }
    result = WorkerResult(
        worker="cursor-worker",
        task="inspect metadata",
        text="done",
        ok=True,
        transport="acpx",
        requested_model=credential_shaped_model,
        effective_model="Bearer private-value",
        acpx_version="v" * 129,
        metadata_provenance=metadata_provenance,
        attempts=(
            WorkerAttempt(
                kind="retry",
                worker="cursor-worker",
                task="inspect metadata",
                transport="acpx",
                model=credential_shaped_model,
                reasoning=None,
                started_at="2026-10-13T12:00:00+00:00",
                finished_at="2026-10-13T12:00:01+00:00",
                exit_code=1,
                terminal_reason="failed",
                failure_phase="dispatch",
                failure_kind="provider-error",
                session_id=None,
            ),
        ),
    )

    entry = worker_payload([result])[0]
    synthesis = agent_result_payload(agents.AgentResult(text="done", ok=True, requested_model=credential_shaped_model))

    assert "requested_model" not in entry
    assert "effective_model" not in entry
    assert "acpx_version" not in entry
    assert "model" not in entry["attempts"][0]
    fields = entry["metadata_provenance"]["fields"]
    assert fields["requested_model"]["source"] == "unknown"
    assert fields["requested_model"]["unknown_reason"] == "requested-model-not-recorded"
    assert fields["effective_model"]["source"] == "unknown"
    assert fields["effective_model"]["unknown_reason"] == "effective-model-not-reported"
    assert fields["acpx_version"]["source"] == "unknown"
    assert fields["acpx_version"]["unknown_reason"] == "acpx-version-not-reported"
    assert "requested_model" not in synthesis
    documented_model_id = "grok-4.5[effort=high,fast=true]"
    observed = worker_payload(
        [
            WorkerResult(
                worker="cursor-worker",
                task="inspect metadata",
                text="done",
                ok=True,
                transport="acpx",
                requested_model=documented_model_id,
                effective_model=documented_model_id,
                acpx_version="0.12.0",
            )
        ]
    )[0]
    assert observed["requested_model"] == documented_model_id
    assert observed["effective_model"] == documented_model_id
    assert observed["acpx_version"] == "0.12.0"


def test_acpx_receipt_sanitizes_provider_detail_and_events(tmp_path: Path):
    credential = "ghp_FAKE0123456789abcdefghijklmnopqrstuvwxyz"
    private_path = r"\root\.ssh\id_rsa"
    result = WorkerResult(
        worker="cursor-worker",
        task="inspect diagnostics",
        text="done",
        ok=False,
        detail=f"provider startup failed: {credential} {private_path}",
        stdout=f"provider output: {credential}",
        stderr=f"provider stderr: {credential} {private_path}",
        transport="acpx",
        session_id=credential,
        request_id=private_path,
        safe_events=(
            {"type": credential, "status": credential},
            {"type": "agent_message_chunk", "status": credential},
        ),
        attempts=(
            WorkerAttempt(
                kind="retry",
                worker="cursor-worker",
                task="inspect diagnostics",
                transport="acpx",
                model=None,
                reasoning=None,
                started_at="2026-10-13T00:00:00+00:00",
                finished_at="2026-10-13T00:00:01+00:00",
                exit_code=None,
                terminal_reason=f"provider stopped {credential} {private_path}",
                failure_phase=None,
                failure_kind=None,
                session_id=None,
            ),
        ),
    )

    output_dir = tmp_path / "run"
    recorded = write_worker_logs(output_dir, [result])[0]
    entry = worker_payload([recorded])[0]
    serialized = json.dumps(entry)

    assert credential not in serialized
    assert private_path not in serialized
    assert entry["attempts"][0]["terminal_reason"] == "<redacted diagnostic>"
    assert entry["detail"] == "<redacted diagnostic>"
    assert entry["events"] == [{"type": "agent_message_chunk"}]
    assert entry["session_id"] == "<redacted diagnostic>"
    assert entry["request_id"] == "<redacted diagnostic>"
    assert (output_dir / recorded.stdout_log).read_text() == "<redacted diagnostic>"
    assert (output_dir / recorded.stderr_log).read_text() == "<redacted diagnostic>"


def test_sanitized_configured_model_keeps_other_provenance_fields():
    credential = "ghp_FAKE0123456789abcdefghijklmnopqrstuvwxyz"
    metadata_provenance = {
        "observed_at": "2026-10-13T12:00:00+00:00",
        "fields": {
            "requested_model": {
                "source": "declared",
                "locator": "configured-request",
                "digest": None,
                "unknown_reason": "execution-not-started",
            },
            "effective_model": {
                "source": "observed",
                "locator": "acp-config-option-update",
                "digest": None,
                "unknown_reason": "backend-revision-unavailable",
            },
            "acpx_version": {
                "source": "observed",
                "locator": "acpx-version-output",
                "digest": None,
                "unknown_reason": None,
            },
        },
    }

    entry = worker_payload(
        [
            WorkerResult(
                worker="cursor-worker",
                task="inspect metadata",
                text="done",
                ok=False,
                transport="acpx",
                requested_model=credential,
                effective_model="composer-2.5",
                acpx_version="0.12.0",
                metadata_provenance=metadata_provenance,
            )
        ]
    )[0]

    assert "requested_model" not in entry
    fields = entry["metadata_provenance"]["fields"]
    assert fields["requested_model"]["source"] == "unknown"
    assert fields["requested_model"]["unknown_reason"] == "requested-model-not-recorded"
    assert fields["effective_model"]["source"] == "observed"
    assert fields["acpx_version"]["source"] == "observed"


def test_non_acp_model_display_labels_survive_receipt_serialization():
    label = "Gemini 3.8 Flash (Low)"
    result = WorkerResult(
        worker="coder",
        task="inspect metadata",
        text="done",
        ok=True,
        transport="cli",
        requested_model=label,
        effective_model=label,
        attempts=(
            WorkerAttempt(
                kind="retry",
                worker="coder",
                task="inspect metadata",
                transport="cli",
                model=label,
                reasoning=None,
                started_at="2026-10-13T12:00:00+00:00",
                finished_at="2026-10-13T12:00:01+00:00",
                exit_code=0,
                terminal_reason="complete",
                failure_phase=None,
                failure_kind=None,
                session_id=None,
            ),
        ),
    )

    worker = worker_payload([result])[0]
    synthesis = agent_result_payload(
        agents.AgentResult(transport="cli", text="done", ok=True, requested_model=label, effective_model=label)
    )

    assert worker["requested_model"] == label
    assert worker["effective_model"] == label
    assert worker["attempts"][0]["model"] == label
    assert synthesis["requested_model"] == label
    assert synthesis["effective_model"] == label


def test_worker_receipt_sinks_drop_known_credential_families_and_version_metadata():
    credential_models = (
        "ghp_" + "A" * 24,
        "github_pat_" + "A" * 24,
        "xoxb-" + "A" * 16,
        "sk-" + "A" * 24,
        "AKIA" + "A" * 16,
        "ASIA" + "A" * 16,
        "AIza" + "a" * 30,
        "glpat-" + "a" * 20,
        "eyJ" + "A" * 8 + "." + "B" * 8 + "." + "C" * 8,
    )
    for model_id in credential_models:
        result = WorkerResult(
            worker="cursor-worker",
            task="inspect metadata",
            text="done",
            ok=True,
            transport="acpx",
            requested_model=model_id,
            effective_model=model_id,
            acpx_version="1.2.3+sk-proj-" + "A" * 24,
            attempts=(
                WorkerAttempt(
                    kind="retry",
                    worker="cursor-worker",
                    task="inspect metadata",
                    transport="acpx",
                    model=model_id,
                    reasoning=None,
                    started_at="2026-10-13T12:00:00+00:00",
                    finished_at="2026-10-13T12:00:01+00:00",
                    exit_code=1,
                    terminal_reason="failed",
                    failure_phase="dispatch",
                    failure_kind="provider-error",
                    session_id=None,
                ),
            ),
        )

        entry = worker_payload([result])[0]
        synthesis = agent_result_payload(
            agents.AgentResult(text="done", ok=True, requested_model=model_id, effective_model=model_id)
        )
        assert "requested_model" not in entry
        assert "effective_model" not in entry
        assert "acpx_version" not in entry
        assert "model" not in entry["attempts"][0]
        assert "requested_model" not in synthesis
        assert "effective_model" not in synthesis


def test_legacy_run_message_displays_unknown_provenance_and_is_not_replayed():
    env, display = message_envelope.synthesize_legacy_message_provenance()
    assert display == "UNKNOWN PROVENANCE - legacy message"
    assert env["trust"]["label"] == "unknown"
    assert env["hashes"]["content"] is None
    assert env["hashes"]["content_scope"] == "message.text.utf8.v1"
    admission = message_envelope.admit_message(
        "legacy body that must not replay",
        env,
        kind="worker-result",
        producer="run_transport.dispatch",
        run_id="demo-run",
        message_id="legacy-message",
        assignment_id="assignment",
        from_seat="coder",
        to_seat="chef",
    )
    assert admission.delivered is False
    assert admission.display == display
    result = WorkerResult(worker="coder", task="implement it", text="legacy body that must not replay", ok=True)
    from brigade import aboyeur

    prompt = aboyeur.build_synth_prompt("build feature", [result], run_id="demo-run", to_seat="chef")
    assert "legacy body that must not replay" not in prompt
    assert display in prompt


def test_pending_and_error_scan_messages_are_not_delivered():
    delivery = message_envelope.emit(
        "implementation output",
        kind="worker-result",
        producer="run_transport.dispatch",
        from_seat="coder",
        to_seat="chef",
        run_id="demo-run",
    )
    for status in ("pending", "error"):
        env = json.loads(json.dumps(delivery.envelope))
        env["trust"]["injection"]["status"] = status
        admission = message_envelope.admit_message(
            "implementation output",
            env,
            kind="worker-result",
            producer="run_transport.dispatch",
            **delivery.envelope["message"],
        )
        assert admission.delivered is False
        assert status in admission.reason


def test_admit_message_rejects_forged_upgraded_labels_without_authority():
    delivery = message_envelope.emit(
        "implementation output",
        kind="worker-result",
        producer="run_transport.dispatch",
        from_seat="coder",
        to_seat="chef",
        run_id="demo-run",
    )
    for label in ("reviewed", "verified"):
        env = json.loads(json.dumps(delivery.envelope))
        env["trust"]["label"] = label
        admission = message_envelope.admit_message(
            "implementation output",
            env,
            kind="worker-result",
            producer="run_transport.dispatch",
            **delivery.envelope["message"],
        )
        assert admission.delivered is False
        assert "authority" in admission.reason or "not deliverable" in admission.reason
        assert f"[envelope trust.label={label}]" not in message_envelope.wrap_message_body("implementation output", env)


def test_worker_payload_and_logs_truncate_beyond_declared_bounds(tmp_path: Path):
    from brigade import agents

    oversized_text = "T" * (message_envelope.MESSAGE_WRAP_MAX_BYTES + 128)
    oversized_stdout = "S" * (proc.MAX_CAPTURE_BYTES + 128)
    result = WorkerResult(
        worker="coder",
        task="implement it",
        text=oversized_text,
        ok=False,
        detail="D" * 32,
        stdout=oversized_stdout,
        stderr="e",
        failure_phase="harness",
        failure_kind="output-limit",
    )
    recorded = write_worker_logs(tmp_path, [result])[0]
    entry = worker_payload([recorded])[0]
    assert len(entry["text"].encode("utf-8")) == message_envelope.MESSAGE_WRAP_MAX_BYTES
    assert len((tmp_path / recorded.stdout_log).read_bytes()) == proc.MAX_CAPTURE_BYTES
    assert len((tmp_path / recorded.stderr_log).read_bytes()) <= proc.MAX_CAPTURE_BYTES

    agent = write_agent_logs(
        tmp_path,
        "planner",
        agents.AgentResult(text=oversized_text, ok=True, stdout=oversized_stdout, stderr="e"),
    )
    assert len(agent.text.encode("utf-8")) == message_envelope.MESSAGE_WRAP_MAX_BYTES
    assert len((tmp_path / agent.stdout_log).read_bytes()) == proc.MAX_CAPTURE_BYTES


def test_acpx_receipt_details_stay_within_byte_cap(tmp_path: Path):
    multibyte_detail = "😃" * (proc.MAX_CAPTURE_BYTES // 4 + 1)
    result = WorkerResult(
        worker="coder",
        task="inspect diagnostics",
        text="result",
        ok=False,
        detail=multibyte_detail,
        stdout=None,
        stderr="",
        transport="acpx",
    )

    recorded = write_worker_logs(tmp_path, [result])[0]
    payload = worker_payload([recorded])[0]

    assert len(payload["detail"].encode("utf-8")) <= proc.MAX_CAPTURE_BYTES
