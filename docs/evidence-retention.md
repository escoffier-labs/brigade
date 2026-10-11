# Captured retention and hold observations

`brigade.evidence_retention.evaluate_retention` evaluates caller-admitted
observations for one acknowledged evidence batch and destination object version.
It returns a frozen `RetentionAssessment`. It performs no I/O or clock lookup
and does not authenticate sources, apply storage settings, archive, restore, or
delete evidence.

## Admission boundary

The caller obtains `AckAcceptance` through the existing acknowledgement
composition and admits its provenance. An accepted acknowledgement is an
authorized receipt assertion. This evaluator also requires its custody evidence
to supply the exact destination reference and object version. It reconciles that
evidence with the acceptance summary and `AckAssociation`, including the request,
envelope, nonce, destination, audience, manifest, verification run and receipt.
It does not repeat cryptographic verification or replay admission.

Each `ObservationBinding` carries the exact `RetentionSubject`, source ID,
source class, observation time, validity window, evidence digest, policy revision
and hold revision. The caller independently supplies the current revisions and
evaluation time. A digest identifies supplied evidence. It cannot authenticate a
source or establish that an operation occurred.

`RetentionDeclaration` uses source class `declared`. Enforcement, hold, recovery
and authority observations require `independent-operating-evidence`. A declaration
cannot supply an independently observed storage control. Actual storage
enforcement and successful restoration require admitted adopter evidence.
The caller must preserve that evidence and map existing archive/package results
to these captured observations. Package integrity alone establishes neither
retention enforcement nor successful restoration.

## Inputs and results

| Input | Meaning | Supported captured state |
| --- | --- | --- |
| `RetentionDeclaration` | Selected calendar period and its explicit start | `declared` in the assessment |
| `EnforcementObservation` | Independently observed retention horizon | `enforced` with `retain_until` |
| `HoldObservation` | Hold state under the supplied current revision | `unheld` or explicitly `released` |
| `RecoveryObservation` as `archive` | Existing archive result reconciled with the subject | `verified` |
| `RecoveryObservation` as `restore` | Actual restoration evidence reconciled with the subject | `verified` |
| `OperationAuthority` | A principal's explicit write, hold, restore or delete permission | `allowed` or `denied` |

Controls also preserve `unknown`, `unavailable`, `indeterminate`, and `failed`
observations. An `active` hold blocks the prerequisite. Assessment states
`mismatch`, `stale`, and `indeterminate` describe binding or validation failures.
`observed_state` preserves the supplied bounded control state when the binding is
stale or mismatched. Missing controls report `unavailable`. Malformed outer input
types raise the bounded error `invalid retention input type`. Malformed nested
values cannot produce a supported prerequisite or inject diagnostic text.

All bindings require a valid window with
`valid_from <= observed_at < valid_until` and
`acknowledged_at <= observed_at <= evaluated_at < valid_until`.
The upper boundary is exclusive. A changed policy or hold revision invalidates
old observations, including an old released hold. A released observation is
caller-admitted evidence of release at that revision, not an inferred transition.
The evaluator does not maintain hold history or discover a newer revision.

The result retains each validated binding, operation authority, selected period
and start, declared expiry, observed retention date, and assessment reasons.
It preserves a writer with explicit denied delete authority alongside a separate
principal with allowed delete authority. It never infers permissions or
independence from principal names. Duplicate observations for the same principal
and operation are ambiguous. Unknown, stale, mismatched or malformed supplied
authority observations block the prerequisite, even when another principal has
an allowed permission. Write and hold permissions are recorded explicitly.
Allowed restore and delete observations are required for the prerequisite.

## Calendar periods and expiry

`calendar_expiry` accepts positive integer ISO calendar year/month/day periods,
including `P6M`, `P18M`, `P7Y`, `P1D`, and `P1Y1M1D`. It combines years and months,
clamps to the destination month's last day, then adds days. It preserves the UTC
time of day. Inputs use whole-second UTC timestamps. Zero periods, negative or
fractional periods, weeks, time components, malformed timestamps, and date
overflow raise `invalid retention calendar`.

```python
from brigade.evidence_retention import calendar_expiry

assert calendar_expiry("2023-08-31T12:34:56Z", "P6M") == "2024-02-29T12:34:56Z"
assert calendar_expiry("2024-08-31T12:34:56Z", "P6M") == "2025-02-28T12:34:56Z"
```

The organization selects the period and start. There is no default period or
universal floor. An observed enforced date earlier than the declaration is a
mismatch. A later observed date extends `expires_at`. The result keeps
`declared_expires_at` and `observed_retain_until` separate. `retention_state`
reports `retained`, `expired` at or after `expires_at`, or `indeterminate` when
the dates cannot be evaluated. Expired retention and stale observation evidence
are separate states. Expired retention cannot compensate for stale controls.

## Synthetic evaluation

Assume `accepted_ack` is a caller-admitted `AckAcceptance` for an acknowledgement
dated `2024-08-31T00:00:00Z`, with independent custody evidence for the exact
object version. The following values describe synthetic captured observations,
not a storage adapter or evidence that these operations occurred:

```python
from dataclasses import replace
from brigade.evidence_retention import (
    EnforcementObservation, HoldObservation, ObservationBinding,
    OperationAuthority, RecoveryObservation, RetentionDeclaration,
    RetentionSubject, evaluate_retention,
)

assert accepted_ack.association is not None
assert accepted_ack.destination_reference is not None
assert accepted_ack.object_version is not None
subject = RetentionSubject(
    accepted_ack.association, accepted_ack.destination_reference,
    accepted_ack.object_version,
)
binding = ObservationBinding(
    subject, "control-a", "independent-operating-evidence",
    "2025-02-28T00:00:00Z", "2025-02-28T00:00:00Z",
    "2025-03-01T00:00:00Z", "policy-1", "hold-1", "8" * 64,
)
result = evaluate_retention(
    acknowledgement=accepted_ack, subject=subject,
    policy_revision="policy-1", hold_revision="hold-1",
    evaluated_at="2025-02-28T01:00:00Z",
    declaration=RetentionDeclaration(
        replace(binding, source_class="declared"),
        "2024-08-31T00:00:00Z", "P6M",
    ),
    enforcement=EnforcementObservation(binding, "enforced", "2025-02-28T00:00:00Z"),
    hold=HoldObservation(binding, "unheld"),
    archive=RecoveryObservation(binding, "verified"),
    restore=RecoveryObservation(binding, "verified"),
    authorities=(
        OperationAuthority(binding, "writer-a", "write", "allowed"),
        OperationAuthority(binding, "writer-a", "delete", "denied"),
        OperationAuthority(binding, "custodian-a", "hold", "allowed"),
        OperationAuthority(binding, "restorer-a", "restore", "allowed"),
        OperationAuthority(binding, "deleter-a", "delete", "allowed"),
    ),
)
assert result.deletion_authorized is False
```

The result is a prospective prerequisite for the future two-phase prune
integration in issue #1622. `prune_prerequisite="supported"` never grants
deletion authorization. This module has no CLI wiring and does not change the
existing archive-before-delete path. Adapter admission, durable recording,
fresh revision discovery, storage enforcement and restoration operations remain
caller integration work. Source IDs and destination references can contain
sensitive metadata. The caller applies existing redaction before persistence or
publication. These observations make no legal certification, compliance claim,
or audit conclusion.
