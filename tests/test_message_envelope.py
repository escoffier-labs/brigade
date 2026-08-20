"""Identity-bound message envelopes refute same-UID transplant (#1010)."""

from __future__ import annotations

from brigade import aboyeur, message_envelope
from brigade.run_transport import WorkerResult


def _emit_worker_result(
    text: str,
    *,
    run_id: str,
    worker: str,
    task: str,
    to_seat: str = "chef",
):
    delivery = message_envelope.emit(
        text,
        kind="worker-result",
        producer="run_transport.dispatch",
        from_seat=worker,
        to_seat=to_seat,
        run_id=run_id,
        assignment_id=message_envelope.assignment_id_for(worker, task),
        session_harness="codex",
    )
    assert delivery.delivered, delivery.reason
    return delivery


def test_emit_binds_run_message_assignment_and_seats():
    delivery = _emit_worker_result(
        "implementation output",
        run_id="run-honest",
        worker="coder",
        task="implement it",
    )
    binding = delivery.envelope["message"]
    assert binding == {
        "run_id": "run-honest",
        "message_id": delivery.message_id,
        "assignment_id": message_envelope.assignment_id_for("coder", "implement it"),
        "from_seat": "coder",
        "to_seat": "chef",
    }
    assert delivery.envelope["session"]["id"] == "run-honest"
    assert delivery.envelope["item_id"] == delivery.message_id


def test_admit_message_requires_expected_identity():
    delivery = _emit_worker_result(
        "implementation output",
        run_id="run-honest",
        worker="coder",
        task="implement it",
    )
    missing = message_envelope.admit_message(
        "implementation output",
        delivery.envelope,
        kind="worker-result",
        producer="run_transport.dispatch",
    )
    assert missing.delivered is False
    assert "expected" in missing.reason


def test_same_uid_transplant_of_valid_envelope_is_rejected():
    """Reproduce the #1010 transplant and confirm the identity gate refutes it.

    Attack: a same-UID seat copies a valid old text/envelope pair into another
    resumable result. Unfixed admission checked only content hash, kind, and a
    generic producer, so the stale body was delivered under the new worker
    identity. This test fails on that unfixed gate (``delivered is True``) and
    passes only when expected ``{run_id, message_id, assignment_id, from_seat,
    to_seat}`` are required and must match the authenticated envelope.
    """

    stale_body = "stale cross-run worker output"
    old = _emit_worker_result(
        stale_body,
        run_id="run-old",
        worker="coder",
        task="implement it",
    )
    honest = message_envelope.admit_message(
        stale_body,
        old.envelope,
        kind="worker-result",
        producer="run_transport.dispatch",
        **old.envelope["message"],
    )
    assert honest.delivered is True

    attack = message_envelope.admit_message(
        stale_body,
        old.envelope,
        kind="worker-result",
        producer="run_transport.dispatch",
        run_id="run-new",
        message_id="worker-result-transplanted",
        assignment_id=message_envelope.assignment_id_for("reviewer", "review it"),
        from_seat="reviewer",
        to_seat="chef",
    )
    assert attack.delivered is False
    assert "does not match expected" in attack.reason

    transplanted = WorkerResult(
        worker="reviewer",
        task="review it",
        text=stale_body,
        ok=True,
        provenance=old.envelope,
    )
    prompt = aboyeur.build_synth_prompt("new run task", [transplanted], run_id="run-new", to_seat="chef")
    assert stale_body not in prompt
    assert "[envelope trust.label=" not in prompt or stale_body not in prompt


def test_unbound_envelope_is_not_admitted_even_with_matching_hash():
    delivery = _emit_worker_result(
        "implementation output",
        run_id="run-honest",
        worker="coder",
        task="implement it",
    )
    unbound = dict(delivery.envelope)
    unbound.pop("message")
    admission = message_envelope.admit_message(
        "implementation output",
        unbound,
        kind="worker-result",
        producer="run_transport.dispatch",
        **delivery.envelope["message"],
    )
    assert admission.delivered is False
    assert "not bound" in admission.reason
