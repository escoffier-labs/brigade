"""Owner-boundary retention evaluation over caller-admitted captured evidence."""

from dataclasses import FrozenInstanceError, replace

import pytest

from brigade import evidence_ack_acceptance as ack
from brigade import evidence_retention as retention

START = "2024-08-31T00:00:00Z"
OBSERVED = "2025-02-28T00:00:00Z"
NOW = "2025-02-28T01:00:00Z"
UNTIL = "2025-03-01T00:00:00Z"


@pytest.fixture
def case():
    # This is a captured acceptance, not a replay of the cryptographic adapter.
    association = ack.AckAssociation(
        "1" * 64, "2" * 64, "3" * 32, "archive-a", "records-a", "4" * 64, "verify-1", "5" * 64
    )
    subject = retention.RetentionSubject(association, "object-a", "version-1")
    custody = ack.AuthorityObservation(
        association, "custody", "supported", "6" * 64, "independent-operating-evidence", "object-a", START, "version-1"
    )
    accepted = ack.AckAcceptance(
        "accepted",
        (),
        association,
        "VALID",
        "MATCH",
        "SHA256:" + "A" * 43,
        "authorized",
        "received",
        "object-a",
        START,
        START,
        "7" * 64,
        "ack-policy-1",
        "independent-operating-evidence",
        "authenticated-assertion",
        "version-1",
        None,
        (custody,),
    )
    binding = retention.ObservationBinding(
        subject,
        "control-a",
        "independent-operating-evidence",
        OBSERVED,
        OBSERVED,
        UNTIL,
        "policy-1",
        "hold-1",
        "8" * 64,
    )
    return dict(
        acknowledgement=accepted,
        subject=subject,
        policy_revision="policy-1",
        hold_revision="hold-1",
        evaluated_at=NOW,
        declaration=retention.RetentionDeclaration(replace(binding, source_class="declared"), START, "P6M"),
        enforcement=retention.EnforcementObservation(binding, "enforced", OBSERVED),
        hold=retention.HoldObservation(binding, "unheld"),
        archive=retention.RecoveryObservation(binding, "verified"),
        restore=retention.RecoveryObservation(binding, "verified"),
        authorities=tuple(
            retention.OperationAuthority(binding, principal, operation, state)
            for principal, operation, state in (
                ("writer-a", "write", "allowed"),
                ("writer-a", "delete", "denied"),
                ("custodian-a", "hold", "allowed"),
                ("restorer-a", "restore", "allowed"),
                ("deleter-a", "delete", "allowed"),
            )
        ),
    )


def evaluate(case, **changes):
    return retention.evaluate_retention(**(case | changes))


@pytest.mark.parametrize("evaluated_at", [OBSERVED, NOW])
def test_supported_prerequisite_keeps_declaration_and_evidence_separate(case, evaluated_at):
    result = evaluate(case, evaluated_at=evaluated_at)
    assert result.prune_prerequisite == "supported"
    assert result.deletion_authorized is False
    assert result.retention_state == "expired" and result.expires_at == OBSERVED
    assert result.declaration.state == "declared"
    assert result.enforcement.state == "enforced"
    assert result.declaration.binding.source_class == "declared"
    assert result.enforcement.binding == case["enforcement"].binding
    assert result.hold.state == "unheld"
    assert result.archive.state == result.restore.state == "verified"
    assert result.reasons == ()
    with pytest.raises(FrozenInstanceError):
        result.prune_prerequisite = "blocked"


@pytest.mark.parametrize("missing", ["declaration", "enforcement", "hold", "archive", "restore"])
def test_missing_observations_block_prerequisite(case, missing):
    result = evaluate(case, **{missing: None})
    assert result.prune_prerequisite == "blocked"
    assert getattr(result, missing).state == "unavailable"


@pytest.mark.parametrize("control", ["declaration", "enforcement", "hold", "archive", "restore"])
@pytest.mark.parametrize(
    ("field", "value", "expected"),
    [
        ("policy_revision", "policy-2", "mismatch"),
        ("hold_revision", "hold-2", "mismatch"),
        ("source_id", "", "indeterminate"),
        ("evidence_sha256", "bad", "indeterminate"),
        ("observed_at", "2025-03-02T00:00:00Z", "indeterminate"),
        ("observed_at", "2024-08-30T00:00:00Z", "indeterminate"),
        ("valid_from", UNTIL, "indeterminate"),
        ("valid_until", NOW, "stale"),
        ("valid_until", "bad", "indeterminate"),
    ],
)
def test_each_observation_checks_its_own_binding(case, control, field, value, expected):
    item = case[control]
    item = replace(item, binding=replace(item.binding, **{field: value}))
    result = evaluate(case, **{control: item})
    assert result.prune_prerequisite == "blocked"
    assert getattr(result, control).state == expected


@pytest.mark.parametrize(
    "field",
    [
        "request_id",
        "envelope_id",
        "nonce",
        "destination_id",
        "audience",
        "manifest_sha256",
        "verify_run_id",
        "receipt_sha256",
    ],
)
def test_control_cannot_be_rebound_to_another_acknowledged_batch(case, field):
    binding = case["enforcement"].binding
    original = getattr(binding.subject.association, field)
    changed = "9" * len(original) if field.endswith("id") and len(original) == 64 else original + "x"
    if field in {"manifest_sha256", "receipt_sha256", "nonce"}:
        changed = "9" * len(original)
    subject = replace(binding.subject, association=replace(binding.subject.association, **{field: changed}))
    result = evaluate(case, enforcement=replace(case["enforcement"], binding=replace(binding, subject=subject)))
    assert result.enforcement.state == "mismatch"
    assert result.prune_prerequisite == "blocked"


@pytest.mark.parametrize("field", ["destination_reference", "object_version"])
@pytest.mark.parametrize("control", ["declaration", "enforcement", "hold", "archive", "restore"])
def test_each_control_requires_exact_object_reference_and_version(case, field, control):
    item = case[control]
    subject = replace(item.binding.subject, **{field: "other"})
    result = evaluate(case, **{control: replace(item, binding=replace(item.binding, subject=subject))})
    assert getattr(result, control).state == "mismatch"
    assert result.prune_prerequisite == "blocked"


@pytest.mark.parametrize("state", ["unknown", "unavailable", "failed", "indeterminate"])
@pytest.mark.parametrize("control", ["enforcement", "archive", "restore"])
def test_failed_or_unestablished_controls_remain_visible(case, state, control):
    result = evaluate(case, **{control: replace(case[control], state=state)})
    assert getattr(result, control).state == state
    assert result.prune_prerequisite == "blocked"


@pytest.mark.parametrize("control", ["enforcement", "hold", "archive", "restore"])
def test_a_declared_setting_cannot_become_operating_evidence(case, control):
    item = case[control]
    result = evaluate(case, **{control: replace(item, binding=replace(item.binding, source_class="declared"))})
    assert getattr(result, control).state == "indeterminate"
    assert result.prune_prerequisite == "blocked"


def test_retention_expiry_and_observation_freshness_are_distinct(case):
    expired = evaluate(
        case, enforcement=replace(case["enforcement"], binding=replace(case["enforcement"].binding, valid_until=NOW))
    )
    assert expired.retention_state == "expired" and expired.enforcement.state == "stale"
    assert expired.prune_prerequisite == "blocked"
    retained = evaluate(
        case,
        declaration=replace(case["declaration"], period="P7Y"),
        enforcement=replace(case["enforcement"], retain_until="2031-08-31T00:00:00Z"),
    )
    assert retained.retention_state == "retained" and retained.enforcement.state == "enforced"
    assert retained.prune_prerequisite == "blocked"


@pytest.mark.parametrize("period", ["P1D", "P6M"])
def test_selected_period_has_no_universal_floor(case, period):
    expires = "2024-09-01T00:00:00Z" if period == "P1D" else OBSERVED
    result = evaluate(
        case,
        declaration=replace(case["declaration"], period=period),
        enforcement=replace(case["enforcement"], retain_until=expires),
    )
    assert result.expires_at == expires and result.prune_prerequisite == "supported"


def test_longer_observed_enforcement_extends_retention_and_shorter_fails(case):
    longer = evaluate(case, enforcement=replace(case["enforcement"], retain_until="2026-08-31T00:00:00Z"))
    assert longer.retention_state == "retained" and longer.expires_at == "2026-08-31T00:00:00Z"
    assert longer.prune_prerequisite == "blocked"
    shorter = evaluate(case, enforcement=replace(case["enforcement"], retain_until=START))
    assert shorter.enforcement.state == "mismatch" and shorter.prune_prerequisite == "blocked"


def test_permissions_preserve_writer_without_delete_and_explicit_operator_authority(case):
    result = evaluate(case)
    assert [(item.principal, item.operation, item.state) for item in result.authorities] == [
        ("writer-a", "write", "allowed"),
        ("writer-a", "delete", "denied"),
        ("custodian-a", "hold", "allowed"),
        ("restorer-a", "restore", "allowed"),
        ("deleter-a", "delete", "allowed"),
    ]
    writer_only = evaluate(case, authorities=case["authorities"][:2])
    assert writer_only.prune_prerequisite == "blocked"
    assert "delete-authority" in writer_only.reasons


@pytest.mark.parametrize("operation", ["write", "hold", "restore", "delete"])
def test_each_permission_requires_independent_current_binding(case, operation):
    items = tuple(
        replace(item, binding=replace(item.binding, policy_revision="old")) if item.operation == operation else item
        for item in case["authorities"]
    )
    result = evaluate(case, authorities=items)
    assert all(item.state == "mismatch" for item in result.authorities if item.operation == operation)
    assert result.prune_prerequisite == "blocked"


def test_duplicate_or_contradictory_permission_is_ambiguous(case):
    items = case["authorities"] + (replace(case["authorities"][-1], state="denied"),)
    result = evaluate(case, authorities=items)
    assert result.authorities[-1].state == result.authorities[-2].state == "ambiguous"
    assert result.prune_prerequisite == "blocked"


@pytest.mark.parametrize(
    "changes",
    [
        {"state": "rejected"},
        {"signature": "INVALID"},
        {"authorization": "rejected"},
        {"object_version": None},
        {"custody_authority": "authenticated-assertion"},
        {"authority": ()},
    ],
)
def test_ack_assertion_without_exact_custody_cannot_support_retention(case, changes):
    result = evaluate(case, acknowledgement=replace(case["acknowledgement"], **changes))
    assert result.prune_prerequisite == "blocked"
    assert "acknowledgement-binding" in result.reasons


@pytest.mark.parametrize(
    "field", ["object_version", "destination_reference", "acknowledged_at", "evidence_sha256", "state"]
)
def test_acceptance_custody_observation_is_reconciled_not_just_its_label(case, field):
    accepted = case["acknowledgement"]
    value = "9" * 64 if field == "evidence_sha256" else "other"
    if field == "evidence_sha256":
        value = "bad"
    observed = replace(accepted.authority[0], **{field: value})
    result = evaluate(case, acknowledgement=replace(accepted, authority=(observed,)))
    assert result.prune_prerequisite == "blocked" and "acknowledgement-binding" in result.reasons


@pytest.mark.parametrize("evaluated_at", [None, "bad", START])
def test_missing_malformed_or_backward_evaluation_time_blocks(case, evaluated_at):
    assert evaluate(case, evaluated_at=evaluated_at).prune_prerequisite == "blocked"


@pytest.mark.parametrize("field", ["period", "starts_at"])
def test_malformed_declaration_blocks_without_crashing(case, field):
    result = evaluate(case, declaration=replace(case["declaration"], **{field: "bad"}))
    assert result.declaration.state == "indeterminate" and result.prune_prerequisite == "blocked"


@pytest.mark.parametrize(
    "field", ["acknowledgement", "subject", "declaration", "enforcement", "hold", "archive", "restore", "authorities"]
)
def test_arbitrary_observation_types_are_rejected_with_bounded_error(case, field):
    with pytest.raises(ValueError, match="^invalid retention input type$"):
        evaluate(case, **{field: {"private": "opaque"}})


def test_malformed_nested_types_do_not_gain_permission_or_leak_diagnostics(case):
    item = replace(case["enforcement"], state=["enforced"])
    result = evaluate(case, enforcement=item)
    assert result.enforcement.state == "indeterminate" and result.prune_prerequisite == "blocked"
    assert result.enforcement.observed_state is None
    assert result.reasons == ("enforcement-indeterminate",)


def test_result_records_selected_period_and_distinct_declared_and_observed_dates(case):
    result = evaluate(case, enforcement=replace(case["enforcement"], retain_until="2026-08-31T00:00:00Z"))
    assert result.starts_at == START and result.period == "P6M"
    assert result.declared_expires_at == OBSERVED
    assert result.observed_retain_until == "2026-08-31T00:00:00Z"
    assert result.expires_at == "2026-08-31T00:00:00Z"


@pytest.mark.parametrize(
    "changes",
    [
        {"operation": ["delete"]},
        {"principal": ["deleter-a"]},
        {"state": ["allowed"]},
        {"binding": None},
    ],
)
def test_malformed_extra_authority_cannot_be_ignored_by_prerequisite(case, changes):
    extra = replace(case["authorities"][-1], **changes)
    result = evaluate(case, authorities=case["authorities"] + (extra,))
    assert result.prune_prerequisite == "blocked"
    assert result.authorities[-1].state == "indeterminate"


@pytest.mark.parametrize("state", ["unknown", "unavailable", "indeterminate"])
def test_unestablished_write_authority_remains_conservative(case, state):
    items = (replace(case["authorities"][0], state=state),) + case["authorities"][1:]
    result = evaluate(case, authorities=items)
    assert result.authorities[0].state == state and result.prune_prerequisite == "blocked"


@pytest.mark.parametrize("field", ["state", "signature", "authorization", "asserted_result", "custody_authority"])
def test_ack_summary_requires_exact_string_types(case, field):
    class Lookalike(str):
        pass

    accepted = case["acknowledgement"]
    result = evaluate(case, acknowledgement=replace(accepted, **{field: Lookalike(getattr(accepted, field))}))
    assert result.prune_prerequisite == "blocked" and "acknowledgement-binding" in result.reasons
