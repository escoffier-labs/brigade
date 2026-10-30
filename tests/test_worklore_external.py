"""Inactive external protocol contracts, with no Hub or persistence acceptance."""

from dataclasses import FrozenInstanceError, replace
from datetime import datetime, timedelta, timezone

import pytest

from brigade import worklore_external as external

NOW = datetime(2026, 1, 1, tzinfo=timezone.utc)
AUTHORIZATION_EPOCH_A = "a" * 32
AUTHORIZATION_EPOCH_B = "b" * 32


def admission_body(**changes):
    return {
        "protocol_version": 1,
        "admission_ref": "evt-offer",
        "work_id": "wl-example",
        "generation": 7,
        "principal": {"kind": "agent", "id": "agent:example"},
        "enrollment_id": "enrollment-a",
        "authorization_enrollment_epoch": AUTHORIZATION_EPOCH_A,
        "repo_identity": "example/project",
        "allowed_paths": ["src/package", "tests"],
        "max_lease_seconds": 300,
        "actions": ["register", "read", "renew", "release", "report"],
        "authorization_ref": "authorization-example",
        "approval_ref": None,
        "campaign_member": False,
        **changes,
    }


def registration_body(**changes):
    return {
        "protocol_version": 1,
        "admission_ref": "evt-offer",
        "work_id": "wl-example",
        "generation": 7,
        "provider": "example-cloud",
        "task_id": "task-a",
        "thread_id": "thread-a",
        "repo_identity": "EXAMPLE/Project",
        "write_scope": ["src/package/module.py", "tests"],
        "source_revision": "a" * 40,
        **changes,
    }


def model():
    admission = external.parse_admission(admission_body())
    binding = external.parse_registration(registration_body())
    lease = external.LeaseSnapshot(admission, binding, 2, "active", NOW, NOW + timedelta(seconds=120))
    observation = external.AuthorityObservation(
        admission.principal,
        "enrollment-a",
        AUTHORIZATION_EPOCH_A,
        external.CredentialBinding("1" * 64),
        external.CredentialBinding("1" * 64),
        None,
        None,
        "active",
    )
    return lease, observation


def request(**changes):
    return external.LeaseRequest(
        **{
            "action": "renew",
            "request_key": "request-a",
            "generation": 7,
            "expected_revision": 2,
            "lease_seconds": 180,
            **changes,
        }
    )


@pytest.mark.parametrize(
    ("paths", "allowed"),
    [
        (["src/package"], True),
        (["src/package/a"], True),
        (["src"], False),
        (["src/packages"], False),
        (["src/Package"], False),
        (["tests/a", "src/package/a"], True),
        (["tests/a", "other"], False),
    ],
)
def test_scope_permission_is_one_way_and_component_based(paths, allowed):
    admission = external.parse_admission(admission_body())
    binding = external.parse_registration(registration_body(write_scope=paths))
    assert external.scope_within_admission(admission, binding) is allowed


@pytest.mark.parametrize(
    ("parser", "changes", "code"),
    [
        ("admission", {"protocol_version": True}, "field-bound"),
        ("admission", {"generation": -1}, "field-bound"),
        ("admission", {"generation": 10**10}, "field-bound"),
        ("admission", {"campaign_member": True}, "campaign-ineligible"),
        ("admission", {"max_lease_seconds": 3601}, "field-bound"),
        ("admission", {"actions": ["register", "force-release"]}, "field-bound"),
        ("admission", {"allowed_paths": ["src"] * 33}, "field-bound"),
        ("admission", {"allowed_paths": []}, "field-bound"),
        ("admission", {"principal": {"kind": "agent", "id": "node-a"}}, "field-bound"),
        ("admission", {"principal": {"kind": "node", "id": "node-a"}}, "principal-forbidden"),
        ("registration", {"source_revision": "A" * 40}, "field-bound"),
        ("registration", {"write_scope": ["src/../other"]}, "field-bound"),
        ("registration", {"write_scope": ["src//other"]}, "field-bound"),
        ("registration", {"write_scope": ["src", "src"]}, "field-bound"),
        ("registration", {"write_scope": "src"}, "field-bound"),
        ("registration", {"task_id": "x" * 129}, "field-bound"),
        ("registration", {"repo_identity": "/absolute"}, "private-data"),
        ("registration", {"thread_id": "password=" + "x" * 20}, "private-data"),
        ("registration", {"actor": "admin"}, "unknown-field"),
        ("registration", {"is_admin": True}, "unknown-field"),
        ("registration", {"password=" + "x" * 20: "private"}, "unknown-field"),
    ],
)
def test_bounded_input_refuses_without_reflecting_untrusted_data(parser, changes, code):
    raw = admission_body(**changes) if parser == "admission" else registration_body(**changes)
    parse = external.parse_admission if parser == "admission" else external.parse_registration
    with pytest.raises(external.ModelError) as caught:
        parse(raw)
    assert caught.value.code == code
    assert str(caught.value) == "external model input refused"
    assert caught.value.__cause__ is None


def test_parser_copies_input_and_first_binding_stays_immutable():
    raw = admission_body()
    admission = external.parse_admission(raw)
    raw["allowed_paths"].append("other")
    raw["principal"]["id"] = "agent:other"
    binding = external.parse_registration(registration_body())
    assert admission.allowed_paths == ("src/package", "tests")
    assert admission.principal.id == "agent:example"
    assert external.validate_binding(admission, binding) == binding
    with pytest.raises(FrozenInstanceError):
        binding.task_id = "other"
    updated_revision = replace(binding, source_revision="b" * 40)
    assert external.validate_binding(admission, updated_revision, pinned=binding) == binding


@pytest.mark.parametrize(
    ("changes", "code"),
    [
        ({"provider": "other"}, "binding-conflict"),
        ({"task_id": "other"}, "binding-conflict"),
        ({"thread_id": "other"}, "binding-conflict"),
        ({"write_scope": ["tests"]}, "binding-conflict"),
        ({"repo_identity": "example/other"}, "admission-mismatch"),
        ({"admission_ref": "evt-other"}, "admission-mismatch"),
        ({"work_id": "wl-other"}, "admission-mismatch"),
        ({"generation": 8}, "stale-generation"),
        ({"write_scope": ["src"]}, "scope-forbidden"),
    ],
)
def test_offer_and_pinned_binding_cannot_be_relabelled(changes, code):
    lease, _ = model()
    candidate = external.parse_registration(registration_body(**changes))
    with pytest.raises(external.ModelError) as caught:
        external.validate_binding(lease.admission, candidate, pinned=lease.binding)
    assert caught.value.code == code


def test_principal_and_epoch_domains_are_separate():
    lease, observed = model()
    assert external.PrincipalRef("node", "example") != external.PrincipalRef("agent", "agent:example")
    # Message grants and mesh keys are observations, not work authority epochs.
    changed = replace(observed, messaging_grant_epoch="m" * 32, mesh_key_epoch=100)
    assert external.decide_lease(lease, changed, request(), holder_matches=True, now=NOW).code == "proposed"
    rotated = replace(
        observed,
        current_credential_generation=external.CredentialBinding("2" * 64),
        authenticated_credential_generation=external.CredentialBinding("2" * 64),
    )
    assert external.decide_lease(lease, rotated, request(), holder_matches=True, now=NOW).code == "proposed"
    assert lease.admission.generation == 7
    assert lease.admission.authorization_enrollment_epoch == AUTHORIZATION_EPOCH_A


@pytest.mark.parametrize(
    ("changes", "holder", "req", "code"),
    [
        ({"activity": "revoked"}, True, {}, "principal-revoked"),
        ({"activity": "inactive"}, True, {}, "principal-inactive"),
        ({"principal": external.PrincipalRef("agent", "agent:other")}, True, {}, "principal-forbidden"),
        ({"enrollment_id": "enrollment-b"}, True, {}, "enrollment-mismatch"),
        ({"authorization_enrollment_epoch": AUTHORIZATION_EPOCH_B}, True, {}, "enrollment-mismatch"),
        ({"current_credential_generation": external.CredentialBinding("2" * 64)}, True, {}, "stale-credential"),
        ({}, False, {}, "holder-mismatch"),
        ({}, True, {"expected_revision": 1}, "version-conflict"),
        ({}, True, {"generation": 8}, "stale-generation"),
        ({}, True, {"lease_seconds": 301}, "lease-duration-forbidden"),
    ],
)
def test_active_decisions_refuse_distinct_stale_authority(changes, holder, req, code):
    lease, observed = model()
    decision = external.decide_lease(
        lease, replace(observed, **changes), request(**req), holder_matches=holder, now=NOW
    )
    assert decision.code == code
    assert decision.snapshot is None
    assert decision.replayed is False


def test_renew_release_and_historical_replay_never_change_a_successor():
    lease, observed = model()
    renewal = request()
    proposed = external.decide_lease(lease, observed, renewal, holder_matches=True, now=NOW)
    assert proposed.code == "proposed" and proposed.snapshot.revision == 3
    assert proposed.snapshot.expires_at == NOW + timedelta(seconds=180)
    assert lease.revision == 2 and lease.expires_at == NOW + timedelta(seconds=120)
    historical = external.HistoricalResult(renewal, proposed.snapshot)
    successor_admission = replace(lease.admission, generation=8, admission_ref="evt-next")
    successor_binding = replace(lease.binding, generation=8, admission_ref="evt-next", task_id="task-b")
    successor = replace(lease, admission=successor_admission, binding=successor_binding, revision=5)
    replay = external.decide_lease(
        successor, observed, renewal, holder_matches=True, now=NOW + timedelta(seconds=60), historical=historical
    )
    assert replay.code == "historical-replay" and replay.replayed
    assert replay.snapshot == proposed.snapshot and replay.snapshot.revision == 3
    assert successor.revision == 5 and successor.binding.task_id == "task-b"
    conflict = external.decide_lease(
        successor, observed, replace(renewal, lease_seconds=181), holder_matches=True, now=NOW, historical=historical
    )
    assert conflict.code == "idempotency-conflict"
    revoked = external.decide_lease(
        successor, replace(observed, activity="revoked"), renewal, holder_matches=True, now=NOW, historical=historical
    )
    assert revoked.code == "principal-revoked" and revoked.snapshot is None
    release = request(action="release", lease_seconds=0)
    released = external.decide_lease(lease, observed, release, holder_matches=True, now=NOW)
    assert released.snapshot.state == "released" and released.snapshot.revision == 3
    historical_release = external.HistoricalResult(release, released.snapshot)
    replay_release = external.decide_lease(
        successor, observed, release, holder_matches=True, now=NOW, historical=historical_release
    )
    assert replay_release.snapshot.state == "released" and successor.state == "active"
    assert (
        external.decide_lease(released.snapshot, observed, renewal, holder_matches=True, now=NOW).code
        == "lease-not-active"
    )


def test_expiry_is_explicit_deterministic_and_never_physical_cessation():
    lease, observed = model()
    assert external.lease_projection(lease, now=NOW)["status"] == "active"
    elapsed = external.lease_projection(lease, now=lease.expires_at)
    assert elapsed["status"] == "elapsed"
    assert elapsed["reservation_retained"] is True
    assert elapsed["physical_writer_stopped"] is False
    assert elapsed["evidence_class"] == "supplied-model-only"
    assert "holder" not in str(elapsed) and "credential" not in str(elapsed)
    for raw in ({}, {"protocol_version": 2}, {"campaign_id": "campaign-example"}, {"expires_at": "bad"}):
        assert external.lease_projection(raw, now=NOW) == {
            "status": "unassessed",
            "reservation_retained": True,
            "physical_writer_stopped": False,
            "evidence_class": "supplied-model-only",
        }
    assert (
        external.decide_lease(lease, observed, request(), holder_matches=True, now=lease.expires_at).code
        == "lease-expired"
    )
    with pytest.raises(external.ModelError):
        external.lease_projection(lease, now=datetime(2026, 1, 1))


def test_ungranted_actions_and_backwards_clock_do_not_propose_mutations():
    lease, observed = model()
    limited = replace(lease, admission=replace(lease.admission, actions=("register", "read")))
    assert external.decide_lease(limited, observed, request(), holder_matches=True, now=NOW).code == "action-forbidden"
    assert (
        external.decide_lease(lease, observed, request(), holder_matches=True, now=NOW - timedelta(seconds=1)).code
        == "clock-before-heartbeat"
    )


@pytest.mark.parametrize("action", ["renew", "release"])
def test_historical_replay_uses_its_own_action_grant_not_successor_permissions(action):
    lease, observed = model()
    req = request(action=action, lease_seconds=180 if action == "renew" else 0)
    proposed = external.decide_lease(lease, observed, req, holder_matches=True, now=NOW).snapshot
    limited = replace(lease.admission, actions=("register", "read"))
    forbidden_history = external.HistoricalResult(req, replace(proposed, admission=limited))
    refused = external.decide_lease(lease, observed, req, holder_matches=True, now=NOW, historical=forbidden_history)
    assert refused.code == "action-forbidden" and refused.snapshot is None
    historical = external.HistoricalResult(req, proposed)
    successor_admission = replace(limited, generation=8, admission_ref="evt-next")
    successor = replace(
        lease,
        admission=successor_admission,
        binding=replace(lease.binding, generation=8, admission_ref="evt-next"),
    )
    replay = external.decide_lease(successor, observed, req, holder_matches=True, now=NOW, historical=historical)
    assert replay.code == "historical-replay" and replay.snapshot == proposed


def test_historical_renewal_window_must_match_the_recorded_request():
    lease, observed = model()
    req = request()
    proposed = external.decide_lease(lease, observed, req, holder_matches=True, now=NOW).snapshot
    inconsistent = replace(proposed, expires_at=NOW + timedelta(seconds=300))
    with pytest.raises(external.ModelError):
        external.HistoricalResult(req, inconsistent)


def test_historical_replay_cannot_cross_repository_identity():
    lease, observed = model()
    req = request()
    proposed = external.decide_lease(lease, observed, req, holder_matches=True, now=NOW).snapshot
    historical = external.HistoricalResult(req, proposed)
    other_repo = replace(
        lease,
        admission=replace(lease.admission, repo_identity="example/other"),
        binding=replace(lease.binding, repo_identity="example/other"),
    )
    refused = external.decide_lease(other_repo, observed, req, holder_matches=True, now=NOW, historical=historical)
    assert refused.code == "idempotency-conflict" and refused.snapshot is None


@pytest.mark.parametrize("activity", ["revoked", "inactive"])
def test_another_principals_activity_is_not_reported_as_the_callers_state(activity):
    lease, observed = model()
    other = replace(observed, principal=external.PrincipalRef("agent", "agent:other"), activity=activity)
    refused = external.decide_lease(lease, other, request(), holder_matches=True, now=NOW)
    assert refused.code == "principal-forbidden" and refused.snapshot is None


@pytest.mark.parametrize("value", [11, True, "short", "a" * 129, "a.b" * 8])
def test_public_authorization_epoch_is_an_opaque_bounded_string(value):
    with pytest.raises(external.ModelError):
        external.parse_admission(admission_body(authorization_enrollment_epoch=value))


def test_private_binding_is_hidden_and_compares_without_public_epoch_coercion():
    binding = external.CredentialBinding("1" * 64)
    assert "1" * 64 not in repr(binding)
    assert binding == external.CredentialBinding("1" * 64)
    assert binding != external.CredentialBinding("2" * 64)
    for value in (23, None, "A" * 64, "1" * 63):
        with pytest.raises(external.ModelError):
            external.CredentialBinding(value)
    _, observed = model()
    with pytest.raises(external.ModelError):
        replace(observed, current_credential_generation=23)


def test_report_metadata_is_bounded_reported_data_with_exact_repo_references():
    report = {
        "version": 1,
        "branch": "feature/example",
        "head_commit": "b" * 40,
        "issues": [{"repo_identity": "example/project", "number": 12}],
        "pull_requests": [{"repo_identity": "example/project", "number": 34}],
    }
    assert external.parse_report(report, repo_identity="example/project") == report
    for changes in (
        {"version": True},
        {"branch": "x" * 129},
        {"branch": "password=" + "x" * 20},
        {"head_commit": "B" * 40},
        {"issues": [{"repo_identity": "example/other", "number": 1}]},
        {"issues": [{"repo_identity": "example/project", "number": True}]},
        {"pull_requests": report["pull_requests"] * 21},
        {"external_key": "forbidden"},
    ):
        with pytest.raises(external.ModelError):
            external.parse_report(report | changes, repo_identity="example/project")
