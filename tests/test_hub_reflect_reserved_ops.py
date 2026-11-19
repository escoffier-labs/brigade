"""A reflect run leaves reserved automatic classes to the principal that can apply them."""

import pytest

from tests.test_memory_reflect_hub import pytestmark as pytestmark, run, setup as setup, writes

POLICY = {"auto_apply": True, "tier0_op_classes": ["merge_exact_duplicates"], "reflect_principals_nonempty": True}


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
        {"policy": {**POLICY, "reflect_principals_nonempty": False}},
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
