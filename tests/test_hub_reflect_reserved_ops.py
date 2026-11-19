"""A reflect run leaves reserved automatic classes to the principal that can apply them."""

import pytest

from brigade import memory_hub_client as hubc, memory_reflect_hub as reflect_hub, memory_reflect_policy as policy
from tests.test_memory_reflect_hub import pytestmark as pytestmark, run, setup as setup, writes

POLICY = {"auto_apply": True, "tier0_op_classes": ["merge_exact_duplicates"], "reserved_scopes": ["fleet"]}


def announce(hub, *, principal=False, policy=POLICY):
    """Make the fake hub's status reply carry what the real one gives a reflect caller."""
    original = hub.do_status

    def status(body):
        code, payload = original(body)
        if principal is not None:
            payload["reflect_principal"] = principal
        if policy is not None:
            payload["memory_policy"] = dict(policy)
        return code, payload

    hub.do_status = status


def test_a_reserved_class_is_left_to_the_listed_principal(setup, capsys):
    target, hub, _ = setup
    announce(hub)
    code, report = run(target, capsys, "run", "--hub")
    assert code == 0
    assert report["skipped_reasons"] == {"reserved_automatic": 1}
    assert report["candidate_count"] == 0 and report["proposals"] == []
    assert writes(hub) == []


def test_a_dry_run_reports_the_same_skip(setup, capsys):
    target, hub, _ = setup
    announce(hub)
    report = run(target, capsys, "run", "--hub", "--dry-run")[1]
    assert report["skipped_reasons"] == {"reserved_automatic": 1} and report["ops"] == []


@pytest.mark.parametrize(
    "kwargs",
    [
        {"principal": None, "policy": None},  # an older hub sends neither field
        {"principal": False, "policy": None},  # no summary for this caller
        {"principal": None},  # summary without the caller's membership
        {"principal": True},  # this caller is listed, so it can apply
        {"policy": {**POLICY, "auto_apply": False}},
        {"policy": {**POLICY, "tier0_op_classes": ["append_evidence"]}},
        {"policy": {**POLICY, "tier0_op_classes": []}},
        {"policy": {**POLICY, "reserved_scopes": []}},
        {"policy": {**POLICY, "reserved_scopes": ["repo:elsewhere"]}},  # no listed principal reflects here
        {"policy": {k: v for k, v in POLICY.items() if k != "reserved_scopes"}},  # the old summary shape
    ],
)
def test_every_other_case_proposes_as_before(setup, capsys, kwargs):
    target, hub, _ = setup
    announce(hub, **kwargs)
    code, report = run(target, capsys, "run", "--hub")
    assert code == 0
    assert "reserved_automatic" not in report["skipped_reasons"]
    assert [call["action"] for call in writes(hub)] == ["propose"]
    assert report["candidate_count"] == 1


def test_a_scope_outside_reserved_scopes_is_still_proposed(setup, capsys):
    target, hub, _ = setup
    # The listed principal reflects on another scope, so this run is the only proposer here.
    announce(hub, policy={**POLICY, "reserved_scopes": ["repo:elsewhere", "repo:other"]})
    code, report = run(target, capsys, "run", "--hub")
    assert code == 0 and report["candidate_count"] == 1
    assert [call["action"] for call in writes(hub)] == ["propose"]


def test_a_mixed_submission_is_still_proposed(setup, capsys, monkeypatch):
    target, hub, _ = setup
    announce(hub)
    exact = reflect_hub._ops
    held = {}

    def mixed(heads):
        ops, skips = exact(heads)
        held["reserved"] = ops[0]
        return ops, skips

    # Learn the real exact op, then hand the near pass a submission pairing it with another class.
    monkeypatch.setattr(reflect_hub, "_ops", mixed)
    assert run(target, capsys, "run", "--hub", "--dry-run")[0] == 0
    other = {**held["reserved"], "type": "supersede_card"}
    monkeypatch.setattr(reflect_hub, "_ops", lambda heads: ([], {}))
    monkeypatch.setattr(reflect_hub.near, "ops", lambda heads, **kw: ([[held["reserved"], other]], {}))
    report = run(target, capsys, "run", "--hub", "--dry-run")[1]
    assert "reserved_automatic" not in report["skipped_reasons"]
    assert report["candidate_count"] == 1 and len(report["ops"]) == 2
    # A submission whose every op is reserved in a reserved scope is skipped.
    monkeypatch.setattr(reflect_hub.near, "ops", lambda heads, **kw: ([[held["reserved"]]], {}))
    assert run(target, capsys, "run", "--hub", "--dry-run")[1]["skipped_reasons"] == {"reserved_automatic": 1}


def test_holds_needs_every_op_reserved_in_a_reserved_scope():
    reserved = reflect_hub._Reserved(frozenset({"merge_exact_duplicates"}), frozenset({"fleet"}))
    one = {"type": "merge_exact_duplicates", "scope": "fleet", "card_id": "a"}
    assert reserved.holds([one])
    assert not reserved.holds([one, {**one, "type": "supersede_card"}])
    assert not reserved.holds([one, {**one, "scope": "repo:other"}])


def test_a_remembered_submission_keeps_its_retry_when_the_class_becomes_reserved(setup, capsys, monkeypatch):
    target, hub, _ = setup
    policy.set_enabled(target, True)
    hub.do_auto_apply = lambda body: (200, {"state": "pending"})
    # Before the upgrade or policy change the run opens the merge and the hub holds it pending.
    assert run(target, capsys, "run", "--scheduled")[0] == 0
    before = len([c for c in writes(hub) if c["action"] == "auto_apply"])
    assert before == 1
    announce(hub)
    code, report = run(target, capsys, "run", "--scheduled")
    assert code == 0
    # The open proposal is retried, not orphaned by a reserved skip.
    assert "reserved_automatic" not in report["skipped_reasons"]
    assert len([c for c in writes(hub) if c["action"] == "auto_apply"]) == before + 1
    assert len([c for c in writes(hub) if c["action"] == "propose"]) == 1


def test_a_remembered_submission_is_not_proposed_again_when_the_class_becomes_reserved(setup, capsys):
    target, hub, _ = setup
    assert run(target, capsys, "run")[0] == 0
    announce(hub)
    report = run(target, capsys, "run")[1]
    # Without automatic apply the remembered proposal is skipped as before, not as reserved.
    assert report["skipped_reasons"] == {"already_proposed": 1}
    assert len(writes(hub)) == 1


class Status:
    def __init__(self, result):
        self.result = result

    def call(self, action, **body):
        if isinstance(self.result, Exception):
            raise self.result
        return self.result


@pytest.mark.parametrize("failure", [hubc.HubUnavailable("down", status=503), hubc.HubRefusal(403, {"code": "no"})])
def test_a_failed_status_call_reserves_nothing(failure):
    reserved = reflect_hub._reserved_classes(Status(failure))
    assert reserved == reflect_hub._Reserved() and not reserved.classes and not reserved.scopes
    assert not reserved.holds([{"type": "merge_exact_duplicates", "scope": "fleet"}])
