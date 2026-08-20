"""Message envelope receipts stay metadata-only and never replay legacy bodies."""

from __future__ import annotations

import json
from pathlib import Path

from brigade import message_envelope, provenance
from brigade.run_receipts import worker_payload
from brigade.run_transport import WorkerResult


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
