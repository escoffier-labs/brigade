"""t3-dispatch tool handlers against a fake runner."""

from __future__ import annotations

import hashlib
from datetime import datetime, timezone

import pytest

from brigade.grokbot_t3 import runtime_config as rc
from brigade.grokbot_t3.fleet import FleetCallError, FleetTimeout, RevisionError
from brigade.grokbot_t3.ledger import Ledger
from brigade.grokbot_t3.tools import DISPATCH_HEADER, T3DispatchTools

from tests.test_grokbot_t3_runtime import runtime_payload

SHA = "0123456789abcdef0123456789abcdef01234567"
NOW = datetime(2026, 10, 16, 12, 0, tzinfo=timezone.utc)


class FakeRunner:
    def __init__(self):
        self.capacity_result = {
            "hosts": [
                {"host": "host-a", "state": "eligible", "active": 10, "limit": 16},
                {"host": "host-b", "state": "eligible", "active": 1, "limit": 12},
                {"host": "host-c", "state": "blocked_offline", "active": 0, "limit": 12},
                {"host": "elsewhere", "state": "eligible", "active": 0, "limit": 99},
            ]
        }
        self.submits = []
        self.submit_errors = []
        self.status_result = {
            "request_id": "r1",
            "host": "host-b",
            "state": "running",
            "needs_attention": False,
            "thread_id": "t1",
            "admission": {"seat": "dsflash", "model": "x"},
            "extra": "dropped",
        }
        self.revision_error = False

    def capacity(self):
        return self.capacity_result

    def resolve_revision(self, remote, branch):
        if self.revision_error:
            raise RevisionError(branch)
        self.last_branch = branch
        return SHA

    def submit(self, **kwargs):
        self.submits.append(kwargs)
        if self.submit_errors:
            raise self.submit_errors.pop(0)
        return {"request_id": f"r{len(self.submits)}", "host": kwargs["host"], "state": "dispatching"}

    def status(self, request_id):
        return dict(self.status_result, request_id=request_id)

    def wait(self, request_id, timeout_s):
        self.waited = timeout_s
        return dict(self.status_result, request_id=request_id)

    def followup(self, request_id, prompt):
        self.followed = prompt
        return dict(self.status_result, request_id=request_id)

    def answer(self, request_id, input_id, answers):
        return dict(self.status_result, request_id=request_id)

    def approve(self, request_id, approval_id, decision):
        return dict(self.status_result, request_id=request_id)

    def interrupt(self, request_id):
        return dict(self.status_result, request_id=request_id, state="interrupted")


@pytest.fixture
def make(tmp_path):
    def _make(**overrides):
        runtime = rc.parse_runtime(runtime_payload(tmp_path, **overrides))
        runner = FakeRunner()
        ledger = Ledger(runtime.ledger_path)
        return T3DispatchTools(runtime=runtime, runner=runner, ledger=ledger, now=lambda: NOW), runner, ledger

    return _make


def _submit(tools, **overrides):
    args = {"repository": "brigade", "prompt": "Do the thing.", "title": "Thing", "caller": "Builder"}
    args.update(overrides)
    return tools.call_tool("t3_submit", args)


def test_submit_picks_host_default_seat_and_records(make):
    tools, runner, ledger = make()
    result = _submit(tools)
    assert result == {
        "request_id": "r1",
        "host": "host-b",
        "seat": "dsflash",
        "state": "dispatching",
        "fallback_used": False,
    }
    sent = runner.submits[0]
    assert sent["host"] == "host-b"
    assert sent["seat"] == "dsflash"
    assert sent["source_revision"] == SHA
    assert sent["title"] == "[grokbot:Builder] Thing"
    assert sent["override_reason"] is None
    assert sent["prompt"] == DISPATCH_HEADER.format(caller="Builder") + "Do the thing."
    row = ledger.get("r1")
    assert row["prompt_sha256"] == hashlib.sha256(sent["prompt"].encode("utf-8")).hexdigest()
    assert "Do the thing." not in str(row)
    assert runner.last_branch == "main"


def test_explicit_host_and_base_ref(make):
    tools, runner, _ = make()
    _submit(tools, host="host-c", base_ref="release/1")
    assert runner.submits[0]["host"] == "host-c"
    assert runner.last_branch == "release/1"


def test_enrolled_picks_allowlisted_host_and_override_reason(make):
    tools, runner, _ = make(fleet_enrolled=True)
    _submit(tools)
    assert runner.submits[0]["host"] == "host-b"
    assert runner.submits[0]["override_reason"] == "grokbot t3-dispatch seat dsflash for Builder"


def test_fallback_seat_on_admission_denied(make):
    tools, runner, _ = make()
    runner.submit_errors = [FleetCallError("policy_rejected", "model-admission-denied")]
    result = _submit(tools)
    assert result["seat"] == "glmflash" and result["fallback_used"] is True
    assert [s["seat"] for s in runner.submits] == ["dsflash", "glmflash"]


def test_named_seat_never_falls_back(make):
    tools, runner, _ = make()
    runner.submit_errors = [FleetCallError("policy_rejected", "model-admission-denied")]
    out = _submit(tools, seat="dsflash")
    assert out["error"]["code"] == "fleet-error"
    assert out["error"]["fleet_reason"] == "model-admission-denied"
    assert len(runner.submits) == 1


@pytest.mark.parametrize(
    "overrides,runtime_overrides,code",
    [
        ({"repository": "other"}, {}, "repository-not-allowed"),
        ({"host": "elsewhere"}, {}, "host-not-allowed"),
        ({"seat": "muse13"}, {}, "seat-not-allowed"),
        ({"prompt": "x" * 2000}, {"prompt_max_bytes": 1024}, "prompt-too-large"),
        ({}, {"enabled": False}, "dispatch-disabled"),
    ],
)
def test_submit_refusals(make, overrides, runtime_overrides, code):
    tools, runner, _ = make(**runtime_overrides)
    out = _submit(tools, **overrides)
    assert out["error"]["code"] == code
    assert runner.submits == []


def test_open_and_daily_limits(make):
    tools, runner, _ = make(max_open=1)
    _submit(tools)
    assert _submit(tools)["error"]["code"] == "open-limit"
    tools, runner, ledger = make(daily_limit=1, max_open=5)
    ledger.update_state("r1", "succeeded", NOW)
    assert _submit(tools)["error"]["code"] == "daily-limit"


def test_no_eligible_host_and_unresolved_ref(make):
    tools, runner, _ = make()
    runner.capacity_result = {"hosts": [{"host": "host-a", "state": "eligible", "active": 16, "limit": 16}]}
    assert _submit(tools)["error"]["code"] == "no-eligible-host"
    tools, runner, _ = make()
    runner.revision_error = True
    assert _submit(tools)["error"]["code"] == "base-ref-unresolved"


def test_submit_timeout_is_unknown(make):
    tools, runner, _ = make()
    runner.submit_errors = [FleetTimeout("submit")]
    assert _submit(tools)["error"]["code"] == "submit-unknown"


def test_status_requires_ledger_entry_and_projects(make):
    tools, runner, ledger = make()
    assert tools.call_tool("t3_status", {"request_id": "nope"})["error"]["code"] == "unknown-request"
    _submit(tools)
    out = tools.call_tool("t3_status", {"request_id": "r1"})
    assert out == {
        "request_id": "r1",
        "host": "host-b",
        "state": "running",
        "needs_attention": False,
        "thread_id": "t1",
        "seat": "dsflash",
    }
    assert ledger.get("r1")["last_state"] == "running"


def test_followup_answer_approve_interrupt_wait_recent(make):
    tools, runner, ledger = make()
    _submit(tools)
    assert tools.call_tool("t3_followup", {"request_id": "r1", "message": "more"})["state"] == "running"
    assert runner.followed == "more"
    assert (
        tools.call_tool("t3_answer", {"request_id": "r1", "input_id": "q", "answers": {"a": "b"}})["state"] == "running"
    )
    assert (
        tools.call_tool("t3_approve", {"request_id": "r1", "approval_id": "a", "decision": "accept"})["state"]
        == "running"
    )
    assert tools.call_tool("t3_wait", {"request_id": "r1", "timeout_s": 5})["state"] == "running"
    assert runner.waited == 5
    assert tools.call_tool("t3_interrupt", {"request_id": "r1"})["state"] == "interrupted"
    assert ledger.get("r1")["last_state"] == "interrupted"
    recent = tools.call_tool("t3_recent", {"caller": "Builder"})
    assert [row["request_id"] for row in recent["requests"]] == ["r1"]


def test_capacity_filters_to_allowlist(make):
    tools, _, _ = make()
    hosts = tools.call_tool("t3_capacity", {})["hosts"]
    assert [h["host"] for h in hosts] == ["host-a", "host-b", "host-c"]


def test_unknown_tool_and_bad_args(make):
    tools, _, _ = make()
    assert tools.call_tool("t3_nope", {})["error"]["code"] == "invalid_request"
    assert tools.call_tool("t3_status", {"request_id": "bad id"})["error"]["code"] == "invalid_request"


@pytest.mark.parametrize("limits,code", [({"max_open": 1}, "open-limit"), ({"daily_limit": 1}, "daily-limit")])
def test_concurrent_submits_cannot_exceed_limits(make, limits, code, monkeypatch):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier
    from time import sleep

    tools, runner, ledger = make(**limits)
    original = runner.submit
    start = Barrier(2)

    def delayed_submit(**kwargs):
        result = original(**kwargs)
        sleep(0.1)  # Keep the first launch in flight while the other caller checks limits.
        return result

    monkeypatch.setattr(runner, "submit", delayed_submit)

    def launch():
        start.wait(timeout=5)
        return _submit(tools)

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: launch(), range(2)))
    assert len(runner.submits) == 1
    assert sum(result.get("error", {}).get("code") == code for result in results) == 1
    assert ledger.open_count() == 1


@pytest.mark.parametrize(
    "outcome", [FleetCallError("unknown_state", request_id="r9"), {"request_id": "r9", "state": "odd-state"}]
)
def test_unknown_submit_with_handle_is_recorded(make, monkeypatch, outcome):
    tools, runner, ledger = make()

    def submit(**kwargs):
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    monkeypatch.setattr(runner, "submit", submit)
    result = _submit(tools)
    assert result["error"]["code"] == "submit-unknown"
    assert result["error"]["request_id"] == "r9"
    assert ledger.get("r9")["last_state"] == "unknown"
    assert tools.call_tool("t3_recent", {})["requests"][0]["request_id"] == "r9"


def test_timed_out_submit_consumes_daily_attempt(make):
    tools, runner, _ = make(daily_limit=1)
    runner.submit_errors = [FleetTimeout("submit")]
    result = _submit(tools)
    assert result["error"]["code"] == "submit-unknown"
    assert "t3_recent" not in result["error"]["message"]
    assert _submit(tools)["error"]["code"] == "daily-limit"
    assert len(runner.submits) == 1


def test_refused_submit_and_fallback_each_consume_daily_attempt(make):
    tools, runner, _ = make(daily_limit=1)
    runner.submit_errors = [FleetCallError("policy_rejected", "model-admission-denied")]
    assert _submit(tools)["error"]["code"] == "daily-limit"
    assert len(runner.submits) == 1


@pytest.mark.parametrize(
    "name,args",
    [
        ("t3_answer", {"input_id": "q1", "answers": {"q": "a"}}),
        ("t3_approve", {"approval_id": "a1", "decision": "accept"}),
    ],
)
def test_disabled_blocks_answer_and_approve_but_allows_interrupt(make, name, args):
    from dataclasses import replace

    tools, _, ledger = make()
    _submit(tools)
    tools.runtime = replace(tools.runtime, enabled=False)
    assert tools.call_tool(name, {"request_id": "r1", **args})["error"]["code"] == "dispatch-disabled"
    assert tools.call_tool("t3_interrupt", {"request_id": "r1"})["state"] == "interrupted"
    assert ledger.get("r1")["last_state"] == "interrupted"


def test_followup_cannot_reopen_past_open_limit(make):
    tools, runner, ledger = make(max_open=1)
    _submit(tools)
    ledger.update_state("r1", "succeeded", NOW)
    _submit(tools)
    result = tools.call_tool("t3_followup", {"request_id": "r1", "message": "more"})
    assert result["error"]["code"] == "open-limit"
    assert not hasattr(runner, "followed")
    assert ledger.get("r1")["last_state"] == "succeeded"


def test_followup_consumes_daily_attempt_even_on_timeout(make, monkeypatch):
    tools, runner, _ = make(daily_limit=2)
    _submit(tools)

    def timeout(*args):
        raise FleetTimeout("followup")

    monkeypatch.setattr(runner, "followup", timeout)
    assert tools.call_tool("t3_followup", {"request_id": "r1", "message": "more"})["error"]["code"] == "unavailable"
    assert tools.call_tool("t3_followup", {"request_id": "r1", "message": "more"})["error"]["code"] == "daily-limit"


def test_admission_error_with_handle_never_falls_back(make):
    tools, runner, ledger = make()
    runner.submit_errors = [FleetCallError("policy_rejected", "model-admission-denied", "r9")]
    result = _submit(tools)
    assert len(runner.submits) == 1
    assert result["error"]["code"] == "submit-unknown"
    assert ledger.get("r9")["last_state"] == "unknown"
