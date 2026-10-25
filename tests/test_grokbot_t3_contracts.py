"""t3-dispatch closed tool contracts."""

from __future__ import annotations

import pytest

from brigade.grokbot_t3 import contracts as c


def _submit(**overrides):
    base = {"repository": "brigade", "prompt": "Fix the flaky test.", "title": "Fix flake", "caller": "Builder"}
    base.update(overrides)
    return base


def test_tool_set_is_closed():
    assert c.TOOLS == {
        "hub_inbox_list",
        "hub_inbox_ack",
        "hub_message_send",
        "hub_message_status",
        "t3_approve",
        "t3_answer",
        "t3_capacity",
        "t3_followup",
        "t3_interrupt",
        "t3_recent",
        "t3_status",
        "t3_submit",
        "t3_wait",
    }
    assert set(c.PARSERS) == c.TOOLS


def test_submit_defaults():
    parsed = c.parse_submit(_submit())
    assert parsed == {
        "repository": "brigade",
        "prompt": "Fix the flaky test.",
        "title": "Fix flake",
        "caller": "Builder",
        "host": "auto",
        "seat": None,
        "base_ref": None,
        "interaction_mode": "default",
    }


def test_submit_optional_fields():
    parsed = c.parse_submit(_submit(host="host-c", seat="sol61", base_ref="release/1.2", interaction_mode="plan"))
    assert (parsed["host"], parsed["seat"], parsed["base_ref"], parsed["interaction_mode"]) == (
        "host-c",
        "sol61",
        "release/1.2",
        "plan",
    )


@pytest.mark.parametrize(
    "overrides",
    [
        {"extra": "x"},
        {"caller": ""},
        {"caller": "bad\nname"},
        {"title": "two\nlines"},
        {"title": "x" * 121},
        {"prompt": "   "},
        {"prompt": "nul\x00byte"},
        {"repository": "../etc"},
        {"base_ref": "a/../b"},
        {"base_ref": "main.lock"},
        {"interaction_mode": "yolo"},
        {"host": ""},
    ],
)
def test_submit_rejects(overrides):
    with pytest.raises(c.T3DispatchError) as info:
        c.parse_submit(_submit(**overrides))
    assert info.value.code == "invalid_request"


def test_submit_rejects_missing_and_non_object():
    with pytest.raises(c.T3DispatchError):
        c.parse_submit({"repository": "brigade"})
    with pytest.raises(c.T3DispatchError):
        c.parse_submit(["not", "an", "object"])


def test_wait_bounds():
    assert c.parse_wait({"request_id": "r-1"}) == {"request_id": "r-1", "timeout_s": 30}
    assert c.parse_wait({"request_id": "r-1", "timeout_s": 60})["timeout_s"] == 60
    for bad in (0, 61, True, 2.5):
        with pytest.raises(c.T3DispatchError):
            c.parse_wait({"request_id": "r-1", "timeout_s": bad})


def test_answer_and_approve():
    assert c.parse_answer({"request_id": "r-1", "input_id": "q-1", "answers": {"pick": "B"}}) == {
        "request_id": "r-1",
        "input_id": "q-1",
        "answers": {"pick": "B"},
    }
    assert c.parse_answer({"request_id": "r-1", "input_id": "q-1", "answers": {"many": ["a", "b"]}})["answers"] == {
        "many": ["a", "b"],
    }
    with pytest.raises(c.T3DispatchError):
        c.parse_answer({"request_id": "r-1", "input_id": "q-1", "answers": {"k": 3}})
    with pytest.raises(c.T3DispatchError):
        c.parse_answer({"request_id": "r-1", "input_id": "q-1", "answers": {"k": "x" * 9000}})
    assert c.parse_approve({"request_id": "r-1", "approval_id": "a-1", "decision": "accept"})["decision"] == "accept"
    with pytest.raises(c.T3DispatchError):
        c.parse_approve({"request_id": "r-1", "approval_id": "a-1", "decision": "yes"})


def test_recent_defaults_and_bounds():
    assert c.parse_recent({}) == {"caller": None, "limit": 10}
    assert c.parse_recent(None) == {"caller": None, "limit": 10}
    assert c.parse_recent({"caller": "Builder", "limit": 20}) == {"caller": "Builder", "limit": 20}
    with pytest.raises(c.T3DispatchError):
        c.parse_recent({"limit": 21})


def test_public_error_shape():
    error = c.T3DispatchError("fleet-error", fleet_code="policy_rejected", fleet_reason="x", request_id="r-1")
    assert error.public_error() == {
        "error": {
            "code": "fleet-error",
            "message": c.ERROR_MESSAGES["fleet-error"],
            "fleet_code": "policy_rejected",
            "fleet_reason": "x",
            "request_id": "r-1",
        }
    }
    assert c.T3DispatchError("not-a-code").code == "unavailable"


def test_lone_surrogates_are_rejected():
    with pytest.raises(c.T3DispatchError):
        c.parse_submit(_submit(prompt="bad \ud800 text"))
    with pytest.raises(c.T3DispatchError):
        c.parse_submit(_submit(title="bad \udfff"))
    with pytest.raises(c.T3DispatchError):
        c.parse_followup({"request_id": "r1", "message": "\ud800"})


def test_error_fields_are_bounded_and_plain():
    error = c.T3DispatchError("fleet-error", fleet_code="x" * 5000, fleet_reason="a b\nc", request_id="bad id")
    out = error.public_error()["error"]
    assert len(out["fleet_code"]) == c.FIELD_MAX
    assert out["fleet_reason"] == "a-b-c"
    assert "request_id" not in out


@pytest.mark.parametrize(
    "name,args",
    [
        ("hub_inbox_list", {"unacked_only": 1}),
        ("hub_inbox_list", {"limit": 51}),
        ("hub_inbox_ack", {"ids": [1, 1]}),
        ("hub_inbox_ack", {"ids": [True]}),
        ("hub_inbox_ack", {"ids": [2**62]}),
        ("hub_inbox_ack", {"ids": list(range(1, 102))}),
        ("hub_message_status", {"ids": list(range(1, 52))}),
        ("hub_message_send", {"alias": "host-a", "subject": "é" * 101, "body": "b"}),
        ("hub_message_send", {"alias": "host-a", "subject": "s", "body": "é" * 4097}),
        ("hub_message_send", {"to": "host-a", "subject": "s", "body": "b"}),
        ("hub_message_send", {"alias": "../host-a", "subject": "s", "body": "b"}),
        ("hub_message_send", {"alias": "host-a", "subject": "s", "body": "b", "extra": None}),
    ],
)
def test_hub_contract_bounds(name, args):
    with pytest.raises(c.T3DispatchError) as error:
        c.PARSERS[name](args)
    assert error.value.code == "invalid_request"


def test_hub_defaults_and_shared_send_fields():
    assert c.PARSERS["hub_inbox_list"]({}) == {"unacked_only": True, "limit": 20}
    assert c.PARSERS["hub_inbox_ack"]({"ids": list(range(1, 101))})["ids"] == list(range(1, 101))
    parsed = c.PARSERS["hub_message_send"]({"alias": "host-a", "subject": "é" * 100, "body": "é" * 4096})
    assert parsed["alias"] == "host-a" and parsed["priority"] == "normal"
    assert parsed["client_id"] is None and "to" not in parsed
