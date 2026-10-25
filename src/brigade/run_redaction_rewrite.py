"""In-memory chain rebuilding for operator-only journal redaction.

Payload policy and transaction publication stay in run_redaction. The caller
supplies its bounded error class so this module does not import that parent.
"""

from __future__ import annotations

from typing import Any, Sequence

from brigade import run_checkpoint, run_events, run_journal


def rewrite_events(
    events: Sequence[run_journal.RunEvent],
    requests: Sequence[tuple[dict[str, Any], str]],
    *,
    error_type: type[RuntimeError],
) -> tuple[list[run_journal.RunEvent], bytes]:
    """Rebuild prepared redaction requests with checkpoint pairing and bounds."""
    rewritten: list[run_journal.RunEvent] = []
    previous_digest: str | None = None
    total = bytearray()
    idempotency_keys: set[str] = set()
    # Prepare both sides before rebuilding the chain: a checkpoint precedes
    # its fact, and either side may be outside the selected range. Preserve
    # the tagged generic identity even when the checkpoint itself is selected.
    for index, event in enumerate(events):
        payload, idempotency_key = requests[index]
        task_pair = event.payload.get("paired_event_type") in run_events._TASK_EVENT_TYPES
        if event.event_type == run_checkpoint.CHECKPOINT_EVENT_TYPE and (
            task_pair or event.idempotency_key.startswith(run_checkpoint._CHECKPOINT_GENERIC_IDEMPOTENCY_PREFIX)
        ):
            following = events[index + 1] if index + 1 < len(events) else None
            if (
                following is None
                or following.sequence != event.sequence + 1
                or following.event_type != event.payload.get("paired_event_type")
            ):
                raise error_type("rewritten checkpoint pair is incomplete")
            fact_payload, fact_key = requests[index + 1]
            try:
                fact_digest = run_events.request_digest(
                    event_type=following.event_type, payload=fact_payload, idempotency_key=fact_key
                )
                if task_pair:
                    payload = {**payload, "pairing_key": fact_digest}
                idempotency_key = run_checkpoint._checkpoint_idempotency_key(
                    payload["sha256"],
                    paired_event_type=following.event_type,
                    body_kind=payload.get("body_kind"),
                    pairing_key=fact_digest,
                )
            except (run_events.CanonicalizationError, ValueError) as exc:
                raise error_type("rewritten checkpoint identity is invalid") from exc
        if idempotency_key in idempotency_keys:
            raise error_type("rewritten journal idempotency key collision")
        idempotency_keys.add(idempotency_key)
        try:
            envelope = run_events.build_event(
                run_id=event.run_id,
                sequence=event.sequence,
                event_type=event.event_type,
                payload=payload,
                idempotency_key=idempotency_key,
                recorded_at=event.recorded_at,
                previous_digest=previous_digest,
            )
            line = run_events.canonical_bytes(envelope) + b"\n"
        except (run_events.CanonicalizationError, ValueError) as exc:
            raise error_type("rewritten journal canonicalization failed") from exc
        if len(line) > run_events.MAX_LINE_BYTES:
            raise error_type("rewritten journal line exceeds size bound")
        total.extend(line)
        if len(total) > run_checkpoint.MAX_JOURNAL_BYTES:
            raise error_type("rewritten journal exceeds size bound")
        try:
            rewritten_event = run_journal.RunEvent(
                schema=envelope["schema"],
                schema_version=envelope["schema_version"],
                event_id=envelope["event_id"],
                run_id=envelope["run_id"],
                sequence=envelope["sequence"],
                event_type=envelope["event_type"],
                recorded_at=envelope["recorded_at"],
                idempotency_key=envelope["idempotency_key"],
                request_digest=envelope["request_digest"],
                previous_digest=envelope["previous_digest"],
                event_digest=envelope["event_digest"],
                payload=dict(envelope["payload"]),
            )
        except (KeyError, TypeError) as exc:
            raise error_type("rewritten journal envelope construction failed") from exc
        rewritten.append(rewritten_event)
        previous_digest = rewritten_event.event_digest
    return rewritten, bytes(total)
