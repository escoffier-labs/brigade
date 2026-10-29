"""Phase A scripted flow evidence only; no production or cryptographic qualification."""

from dataclasses import replace
import json
import os
import sqlite3

import pytest

from brigade.card_identity import valid_card_id
from tests._memory_contract_proof import (
    A,
    B,
    REVIEWER,
    PUBLISHER,
    ADMIN,
    RECOVERY,
    CHECKPOINT,
    TEAM,
    CANDIDATE,
    PERSONAL,
    MARKERS,
    REFUSAL,
    Authority,
    TrustedClient,
    AdapterA,
    AdapterB,
    publisher_core,
    request_digest,
)


@pytest.fixture
def proof(tmp_path):
    authority = Authority(tmp_path / "fixture")
    client = TrustedClient(authority)
    yield authority, client
    authority.db.close()


def prepare(authority, request="op-1", **changes):
    artifact = replace(authority.artifact(), **changes)
    review = authority.review(REVIEWER, artifact)
    return authority.authorize(PUBLISHER, request, artifact, review)


def counts(authority):
    return tuple(
        authority.db.execute(f"SELECT count(*) FROM {table}").fetchone()[0]
        for table in ("revisions", "outcomes", "audit")
    )


def test_two_consumers_and_denied_surfaces(proof):
    authority, client = proof
    a, b = AdapterA(client), AdapterB(client)
    left, right = a.read(TEAM), b.read(TEAM)
    assert left == right
    assert left["excerpt"] == MARKERS[0]
    assert left["revision"] == "rev-0" and left["citation"] == "synthetic-source:1"
    assert all(valid_card_id(item) == item for item in (TEAM, CANDIDATE, PERSONAL))
    for adapter in (a, b):
        for item in (CANDIDATE, PERSONAL, "unknown"):
            assert adapter.read(item) == REFUSAL
        assert adapter.read(TEAM, sink="cloud:ungranted") == REFUSAL
    assert client.sinks == [("local-client", TEAM, "rev-0")] * 2
    exported = authority.backup().encode() + authority.diagnostics().encode()
    assert all(marker.encode() not in exported for marker in MARKERS)
    assert all(marker not in json.dumps(REFUSAL) for marker in MARKERS)


def test_identity_labels_and_devices_grant_nothing(proof):
    authority, client = proof
    a, b = AdapterA(client), AdapterB(client)
    assert a.propose() == {"state": "local-candidate"}
    assert b.propose() == REFUSAL
    a.persona = "publisher"
    a.label = "admin"
    assert a.propose() == {"state": "local-candidate"}
    assert AdapterA(client, identity=("unknown", "unknown-device")).read(TEAM) == REFUSAL
    assert AdapterA(client, identity=(A[0], B[1])).read(TEAM) == REFUSAL
    for actor in (A, B, ADMIN, CHECKPOINT, RECOVERY):
        assert authority.review(actor, authority.artifact()) is None
        assert authority.commit(actor, prepare(authority)) == REFUSAL
    assert len({actor[0] for actor in (A, B, REVIEWER, PUBLISHER, ADMIN, RECOVERY, CHECKPOINT)}) == 7
    assert len({actor[1] for actor in (A, B, REVIEWER, PUBLISHER, ADMIN, RECOVERY, CHECKPOINT)}) == 7
    assert counts(authority) == (3, 0, 0)


@pytest.mark.parametrize(
    "field,value",
    [
        ("handle", "opaque-substitute"),
        ("base", "other-base"),
        ("sources", ("other-source",)),
        ("scope", "other-scope"),
        ("mutation", "other-mutation"),
        ("recipients", (A[1],)),
        ("epoch", 9),
        ("policy", 9),
    ],
)
def test_review_binding_substitution(proof, field, value):
    authority, _ = proof
    attempt = prepare(authority)
    forged = replace(attempt, artifact=replace(attempt.artifact, **{field: value}))
    # Even a newly scripted publisher decision cannot substitute an old review.
    forged = authority.authorize(PUBLISHER, "op-2", forged.artifact, attempt.review)
    assert authority.commit(PUBLISHER, forged) == REFUSAL
    assert counts(authority) == (3, 0, 0)
    assert authority.head == 0


def test_publisher_owner_and_scoped_id_binding(proof):
    authority, _ = proof
    attempt = prepare(authority)
    for forged in (
        replace(attempt, request="op-other"),
        replace(attempt, owner=ADMIN[0]),
        replace(attempt, scope="other-scope"),
    ):
        assert authority.commit(PUBLISHER, forged) == REFUSAL
    assert authority.commit(ADMIN, attempt) == REFUSAL
    assert counts(authority) == (3, 0, 0)


def test_scripted_failures_and_transaction_order(proof):
    authority, _ = proof
    attempt = prepare(authority)
    authority.verifier.fail = {"review"}
    assert authority.commit(PUBLISHER, attempt) == REFUSAL
    assert authority.verifier.calls == [("review", True)]
    authority.verifier.fail = {"publisher"}
    assert authority.commit(PUBLISHER, attempt) == REFUSAL
    assert authority.verifier.calls[-2:] == [("review", True), ("publisher", True)]
    authority.verifier.fail.clear()
    assert authority.commit(PUBLISHER, attempt)["state"] == "committed"
    assert authority.trace.index("BEGIN IMMEDIATE") < next(
        i for i, s in enumerate(authority.trace) if "FROM grants" in s
    )
    assert counts(authority) == (4, 1, 1)


def test_one_head_two_attempts_and_stale_review(proof):
    authority, _ = proof
    first, second = prepare(authority, "op-a"), prepare(authority, "op-b")
    assert authority.commit(PUBLISHER, first)["state"] == "committed"
    conflict = authority.commit(PUBLISHER, second)
    assert conflict["state"] == "conflict"
    refreshed = authority.authorize(PUBLISHER, "op-c", second.artifact, second.review)
    assert refreshed.head == 1
    assert authority.commit(PUBLISHER, refreshed)["state"] == "conflict"
    assert authority.head == 1 and counts(authority) == (4, 3, 3)


@pytest.mark.parametrize("transition", ["policy", "fence", "epoch", "recipient", "wrap-surgery"])
def test_prepared_mutable_state_rechecked(proof, transition):
    authority, _ = proof
    attempt = prepare(authority)
    if transition in {"recipient", "wrap-surgery"}:
        authority.revoke(B)
        if transition == "wrap-surgery":
            attempt = authority.authorize(
                PUBLISHER, "op-new", replace(attempt.artifact, recipients=(A[1],)), attempt.review
            )
    else:
        authority.advance(transition)
    result = authority.commit(PUBLISHER, attempt)
    expected = "unavailable" if transition == "wrap-surgery" else "conflict"
    assert result["state"] == expected
    assert counts(authority)[1:] == ((0, 0) if transition == "wrap-surgery" else (1, 1))
    assert authority.head == 0
    assert counts(authority)[0] == 3


def test_lost_response_historical_recovery_digest_and_revocation(proof):
    authority, _ = proof
    old = prepare(authority)
    receipt = authority.commit(PUBLISHER, old)  # response deliberately discarded by caller
    assert authority.commit(PUBLISHER, prepare(authority, "op-next"))["state"] == "committed"
    authority.advance("policy")
    authority.advance("fence")
    assert authority.commit(PUBLISHER, old) == receipt
    assert authority.outcome(PUBLISHER, old) == receipt
    assert authority.commit(PUBLISHER, replace(old, digest="changed"))["state"] == "conflict"
    assert counts(authority) == (5, 2, 2)
    authority.revoke(PUBLISHER)
    for actor in (PUBLISHER, ("unknown", "device-x")):
        assert authority.outcome(actor, old) == REFUSAL
        assert authority.commit(actor, old) == REFUSAL
    assert counts(authority) == (5, 2, 2)


@pytest.mark.parametrize("cancel_first", [True, False])
def test_cancel_commit_terminal_orderings(proof, cancel_first):
    authority, _ = proof
    attempt = prepare(authority)
    if cancel_first:
        first = authority.cancel(PUBLISHER, attempt)
        assert first["state"] == "abandoned"
        assert authority.commit(PUBLISHER, attempt) == first
        assert authority.head == 0 and counts(authority) == (3, 1, 1)
    else:
        first = authority.commit(PUBLISHER, attempt)
        assert authority.cancel(PUBLISHER, attempt) == first
        assert authority.head == 1 and counts(authority) == (4, 1, 1)


def test_recovery_can_cancel_without_restoring_owner(proof):
    authority, _ = proof
    attempt = prepare(authority)
    authority.revoke(PUBLISHER)
    assert authority.cancel(PUBLISHER, attempt) == REFUSAL
    assert authority.cancel(ADMIN, attempt) == REFUSAL
    assert authority.cancel(RECOVERY, attempt)["state"] == "abandoned"
    assert authority.commit(PUBLISHER, attempt) == REFUSAL
    assert authority.outcome(PUBLISHER, attempt) == REFUSAL
    assert authority.head == 0 and counts(authority) == (3, 1, 1)


def test_pending_absence_and_preadmission_refusal(proof):
    authority, client = proof
    attempt = prepare(authority)
    client.pending = attempt
    authority.verifier.fail = {"publisher"}
    assert client.retry() == REFUSAL
    assert client.pending == attempt and counts(authority) == (3, 0, 0)
    authority.verifier.fail.clear()
    assert client.reconcile() == {"state": "exact-retry"}
    assert client.pending == attempt
    assert client.retry(replace(attempt, request="fresh-id")) == REFUSAL
    authority.ledger_complete = False
    assert client.reconcile() == {"state": "ambiguous"}
    assert client.retry() == {"state": "ambiguous"}
    authority.ledger_complete = True
    assert client.retry()["state"] == "committed"
    assert client.pending is None and counts(authority) == (4, 1, 1)


def test_atomic_rollback_on_audit_failure(proof):
    authority, _ = proof
    attempt = prepare(authority)
    checkpoint = authority.checkpoints[-1]
    authority.db.execute(
        "CREATE TRIGGER fail_audit BEFORE INSERT ON audit BEGIN SELECT RAISE(ABORT, 'scripted audit failure'); END"
    )
    with pytest.raises(sqlite3.IntegrityError, match="scripted audit failure"):
        authority.commit(PUBLISHER, attempt)
    assert authority.head == 0 and counts(authority) == (3, 0, 0)
    assert authority.checkpoints[-1] == checkpoint


def test_strict_restore_and_independent_checkpoints(proof):
    authority, _ = proof
    assert len(authority.checkpoints) == 1
    old = authority.backup()
    authority.commit(PUBLISHER, prepare(authority))
    assert len(authority.checkpoints) == 2
    current = authority.backup()
    assert authority.restore(RECOVERY, old) == REFUSAL  # no deletion occurred
    assert authority.restore(RECOVERY, None) == REFUSAL
    assert authority.restore(ADMIN, current) == REFUSAL
    authority.ledger_complete = False
    assert authority.restore(RECOVERY, current) == REFUSAL
    authority.ledger_complete = True
    checkpoints = authority.checkpoints[:]
    authority.checkpoints.clear()
    assert authority.restore(RECOVERY, current) == REFUSAL
    authority.checkpoints[:] = checkpoints
    old_attempt = prepare(authority, "before-restore")
    assert authority.restore(RECOVERY, current)["state"] == "restored"
    assert authority.commit(PUBLISHER, old_attempt)["state"] == "ambiguous"


def test_future_only_device_does_not_inherit_history(proof):
    authority, client = proof
    new = ("future-reader", "future-device")
    authority.enroll(ADMIN, new)
    assert client.read(new, TEAM) == REFUSAL
    assert authority.commit(PUBLISHER, prepare(authority))["state"] == "committed"
    assert client.read(new, TEAM)["revision"] == "rev-1"
    assert client.read(new, TEAM, revision="rev-0") == REFUSAL


def test_bounds_and_solo_side_effects(proof, tmp_path):
    authority, client = proof
    assert client.read(A, TEAM, limit=6) == REFUSAL
    assert client.read(A, TEAM, excerpt_bytes=4097) == REFUSAL
    client.contents["opaque-0"] = ("é" * 8193, "synthetic-source:1")
    assert client.read(A, TEAM) == REFUSAL
    client.contents["opaque-0"] = ("é" * 8192, "synthetic-source:1")
    assert len(client.read(A, TEAM)["excerpt"].encode()) <= 4096
    attempt = prepare(authority)
    for forged in (replace(attempt, bundle="x" * 4097), replace(attempt, signatures=3)):
        assert authority.commit(PUBLISHER, forged) == REFUSAL
    too_many = prepare(authority, "many", recipients=tuple(f"device-{i}" for i in range(5)))
    assert authority.commit(PUBLISHER, too_many) == REFUSAL
    assert authority.head == 0
    snapshot = authority.side_effects()
    assert [(name, kind) for name, kind, _ in snapshot] == [(".", "directory"), ("authority.sqlite", "file")]
    if os.name == "posix":
        assert [mode for _, _, mode in snapshot] == [0o700, 0o600]
    assert sorted(p.name for p in tmp_path.iterdir()) == ["fixture"]
    assert authority.profile == "local-solo-fixture"


@pytest.mark.parametrize("actor", [REVIEWER, PUBLISHER])
def test_revoked_promotion_role_cannot_admit(proof, actor):
    authority, _ = proof
    attempt = prepare(authority)
    authority.revoke(actor)
    assert authority.commit(PUBLISHER, attempt) == REFUSAL
    assert authority.head == 0 and counts(authority) == (3, 0, 0)


def test_unknown_and_revoked_read_and_outcome_share_shape(proof):
    authority, client = proof
    attempt = prepare(authority)
    authority.commit(PUBLISHER, attempt)
    authority.revoke(A)
    authority.revoke(PUBLISHER)
    surfaces = [
        client.read(A, TEAM),
        client.read(("unknown", "device-x"), PERSONAL),
        authority.outcome(PUBLISHER, attempt),
        authority.outcome(("unknown", "device-x"), replace(attempt, request="missing")),
    ]
    assert surfaces == [REFUSAL] * 4
    assert all(len(json.dumps(result).encode()) < 100 for result in surfaces)
    assert all(marker not in json.dumps(surfaces) for marker in MARKERS)


def test_restore_rejects_missing_ledger_and_restored_absence_is_not_replayed(proof):
    authority, client = proof
    pending = prepare(authority)
    client.pending = pending
    backup = authority.backup()
    with sqlite3.connect(":memory:") as damaged:
        damaged.executescript(backup)
        damaged.execute("DROP TABLE outcomes")
        assert authority.restore(RECOVERY, "\n".join(damaged.iterdump())) == REFUSAL
    assert authority.restore(RECOVERY, backup)["state"] == "restored"
    # The client retains the original pending identity across restore.
    assert client.retry(replace(pending, request="fresh-after-restore")) == REFUSAL
    assert client.pending == pending
    assert client.retry() == {"state": "ambiguous"}
    assert client.pending == pending
    assert authority.head == 0 and counts(authority) == (3, 0, 0)


def test_restored_absence_remains_ambiguous(proof):
    authority, client = proof
    client.pending = prepare(authority)
    authority.restore(RECOVERY, authority.backup())
    assert client.reconcile() == {"state": "ambiguous"}
    assert client.retry() == {"state": "ambiguous"}
    assert client.pending is not None
    assert authority.head == 0 and counts(authority) == (3, 0, 0)


def resign(authority, attempt):
    """Issue a fresh scripted publisher decision to isolate other guards."""
    authority.verifier.publishers[attempt.publisher] = publisher_core(attempt)
    return replace(attempt, digest=request_digest(attempt))


def test_new_epoch_can_publish_while_old_absence_stays_ambiguous(proof):
    authority, client = proof
    client.pending = prepare(authority, "old-pending")
    assert authority.restore(RECOVERY, authority.backup())["state"] == "restored"
    fresh = prepare(authority, "new-epoch")
    assert fresh.epoch == 1
    assert authority.commit(PUBLISHER, fresh)["state"] == "committed"
    assert client.reconcile() == {"state": "ambiguous"}
    assert client.pending is not None


@pytest.mark.parametrize("terminal", ["abandoned", "conflict"])
def test_restore_cannot_drop_ledger_only_terminal(proof, terminal):
    authority, _ = proof
    attempt = prepare(authority)
    before = authority.backup()
    if terminal == "abandoned":
        outcome = authority.cancel(PUBLISHER, attempt)
    else:
        authority.db.execute("UPDATE meta SET head=1")
        outcome = authority.commit(PUBLISHER, attempt)
    assert outcome["state"] == terminal
    assert authority.restore(RECOVERY, before) == REFUSAL
    assert authority.outcome(PUBLISHER, attempt) == outcome


def test_checkpoint_gap_cannot_restore_revoked_access(proof):
    authority, client = proof
    before = authority.backup()
    authority.revoke(CHECKPOINT)
    authority.revoke(B)
    assert client.read(B, TEAM) == REFUSAL
    assert authority.restore(RECOVERY, before) == REFUSAL
    assert client.read(B, TEAM) == REFUSAL


def test_recovery_lookup_without_restoring_publisher_privileges(proof):
    authority, client = proof
    attempt = prepare(authority)
    receipt = authority.commit(PUBLISHER, attempt)
    authority.revoke(PUBLISHER)
    assert authority.outcome(PUBLISHER, attempt) == REFUSAL
    assert authority.outcome(RECOVERY, attempt) == receipt
    assert client.read(RECOVERY, TEAM) == REFUSAL
    assert authority.commit(RECOVERY, prepare(authority, "other")) == REFUSAL


@pytest.mark.parametrize(
    "field,value",
    [("bundle", "x" * 5000), ("signatures", 3), ("artifact", None)],
    ids=["bundle", "signatures", "recipients"],
)
def test_structural_bounds_reject_before_write_lock(proof, field, value):
    authority, _ = proof
    attempt = prepare(authority)
    if field == "artifact":
        value = replace(attempt.artifact, recipients=tuple(f"device-{n}" for n in range(5)))
    attempt = resign(authority, replace(attempt, **{field: value}))
    authority.ledger_complete = False
    authority.trace.clear()
    assert authority.commit(PUBLISHER, attempt) == REFUSAL
    assert "BEGIN IMMEDIATE" not in authority.trace


@pytest.mark.parametrize("field,value", [("request", "other-id"), ("scope", "other-scope")])
def test_public_digest_cannot_replace_publisher_binding(proof, field, value):
    authority, _ = proof
    attempt = replace(prepare(authority), **{field: value})
    attempt = replace(attempt, digest=request_digest(attempt))
    assert authority.commit(PUBLISHER, attempt) == REFUSAL
    assert authority.verifier.calls[-2:] == [("review", True), ("publisher", True)]


def test_other_granted_publisher_cannot_act_as_operation_owner(proof):
    authority, _ = proof
    other = ("other-publisher", "other-publisher-device")
    authority.db.execute("INSERT INTO grants VALUES (?, ?, 'publish', 0, 1)", other)
    assert authority.commit(other, prepare(authority)) == REFUSAL
    assert counts(authority)[1:] == (0, 0)


@pytest.mark.parametrize("guard", ["head", "recipient"])
def test_mutable_guards_without_policy_or_item_base_change(proof, guard):
    authority, _ = proof
    attempt = prepare(authority)
    if guard == "head":
        authority.db.execute("UPDATE meta SET head=1")
    else:
        authority.db.execute("DELETE FROM grants WHERE principal=?", (B[0],))
    assert authority.commit(PUBLISHER, attempt)["state"] == "conflict"
    assert counts(authority)[1:] == (1, 1)


def test_read_wrap_membership_and_history_are_independent(proof):
    authority, client = proof
    authority.db.execute("UPDATE revisions SET recipients=? WHERE item=?", (json.dumps((B[1],)), TEAM))
    assert client.read(A, TEAM) == REFUSAL
    new = ("future-reader", "future-device")
    authority.enroll(ADMIN, new)
    authority.db.execute("UPDATE revisions SET recipients=? WHERE item=?", (json.dumps((B[1], new[1])), TEAM))
    assert client.read(new, TEAM) == REFUSAL


def test_unapproved_and_private_items_have_real_payloads(proof):
    authority, client = proof
    assert authority.db.execute("SELECT count(*) FROM revisions").fetchone()[0] == 3
    assert client.read(A, CANDIDATE) == REFUSAL
    authority.db.execute("UPDATE items SET approved=1 WHERE item=?", (CANDIDATE,))
    assert client.read(A, CANDIDATE)["excerpt"] == MARKERS[1]
    assert client.read(A, PERSONAL) == REFUSAL
    authority.db.execute("UPDATE items SET owner=NULL WHERE item=?", (PERSONAL,))
    assert client.read(A, PERSONAL)["excerpt"] == MARKERS[2]


def test_local_disclosure_callback_is_live_but_cloud_is_not(proof):
    _, client = proof
    assert client.read(A, TEAM)["excerpt"] == MARKERS[0]
    assert client.sinks == [("local-client", TEAM, "rev-0")]
    assert client.read(A, TEAM, sink="cloud:ungranted") == REFUSAL
    assert client.sinks == [("local-client", TEAM, "rev-0")]


def test_request_digest_is_checked_separately_from_scripted_signature(proof):
    authority, _ = proof
    attempt = replace(prepare(authority), digest="invalid-public-digest")
    assert authority.commit(PUBLISHER, attempt) == REFUSAL
    assert authority.verifier.calls == []


def test_original_publisher_signer_revocation_with_other_active_device(proof):
    authority, _ = proof
    attempt = prepare(authority)
    alternate = (PUBLISHER[0], "publisher-alternate-device")
    authority.db.execute("INSERT INTO grants VALUES (?, ?, 'publish', 0, 1)", alternate)
    authority.revoke(PUBLISHER)
    assert authority.commit(alternate, attempt) == REFUSAL
    assert authority.verifier.calls[-2:] == [("review", True), ("publisher", True)]
    assert counts(authority)[1:] == (0, 0)


@pytest.mark.parametrize("epoch_field", ["operation", "artifact"])
def test_each_epoch_binding_is_current_independently(proof, epoch_field):
    authority, _ = proof
    if epoch_field == "operation":
        attempt = resign(authority, replace(prepare(authority), epoch=1))
    else:
        attempt = prepare(authority, epoch=1)
    assert authority.commit(PUBLISHER, attempt)["state"] == "conflict"
    assert authority.head == 0
