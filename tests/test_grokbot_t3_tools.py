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

    def followup(self, request_id, prompt, interaction_mode="default"):
        self.followed_mode = interaction_mode
        self.followed = prompt
        return dict(self.status_result, request_id=request_id)

    def answer(self, request_id, input_id, answers):
        return dict(self.status_result, request_id=request_id)

    def approve(self, request_id, approval_id, decision):
        return dict(self.status_result, request_id=request_id)

    def interrupt(self, request_id):
        return dict(self.status_result, request_id=request_id, state="interrupted")


@pytest.fixture
def make(tmp_path, t3_ledger_storage):
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


def test_unreadable_submit_output_is_unknown_not_refused(make):
    from brigade.grokbot_t3.fleet import FleetProtocolError

    tools, runner, ledger = make()
    runner.submit_errors = [FleetProtocolError("internal_error")]
    out = _submit(tools)
    assert out["error"]["code"] == "submit-unknown"


def test_unreadable_submit_output_with_request_id_is_recorded(make):
    from brigade.grokbot_t3.fleet import FleetProtocolError

    tools, runner, ledger = make()
    runner.submit_errors = [FleetProtocolError("internal_error", None, "host-a-r9")]
    _submit(tools)
    assert ledger.get("host-a-r9")["last_state"] == "unknown"


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
    assert tools.call_tool("t3_followup", {"request_id": "r1", "message": "more"})["error"]["code"] == "submit-unknown"
    assert tools.call_tool("t3_followup", {"request_id": "r1", "message": "more"})["error"]["code"] == "daily-limit"


def test_admission_error_with_handle_never_falls_back(make):
    tools, runner, ledger = make()
    runner.submit_errors = [FleetCallError("policy_rejected", "model-admission-denied", "r9")]
    result = _submit(tools)
    assert len(runner.submits) == 1
    assert result["error"]["code"] == "submit-unknown"
    assert ledger.get("r9")["last_state"] == "unknown"


@pytest.mark.parametrize("action", ["submit", "followup"])
def test_contended_dispatch_returns_unavailable_without_launching(make, action):
    from threading import Thread
    from time import monotonic

    tools, runner, ledger = make()
    if action == "followup":
        _submit(tools)
    before = len(runner.submits)
    attempts = ledger.attempts_since(NOW.replace(hour=0))
    results = []

    def call():
        started = monotonic()
        result = (
            _submit(tools)
            if action == "submit"
            else tools.call_tool("t3_followup", {"request_id": "r1", "message": "more"})
        )
        results.append((result, monotonic() - started))

    tools._dispatch_lock.acquire()
    thread = Thread(target=call)
    try:
        thread.start()
        thread.join(timeout=5.8)
        finished_while_locked = not thread.is_alive()
    finally:
        tools._dispatch_lock.release()
        thread.join(timeout=2)
    assert finished_while_locked
    assert results[0][0]["error"]["code"] == "unavailable"
    assert results[0][1] < 5.8
    assert len(runner.submits) == before
    assert not hasattr(runner, "followed")
    assert ledger.attempts_since(NOW.replace(hour=0)) == attempts


@pytest.mark.parametrize("mode", ["default", "plan"])
def test_followup_preserves_submitted_interaction_mode_after_reopen(make, mode):
    tools, runner, ledger = make()
    _submit(tools, interaction_mode=mode)
    ledger.close()
    tools.ledger = Ledger(tools.runtime.ledger_path)
    result = tools.call_tool("t3_followup", {"request_id": "r1", "message": "more"})
    assert result["state"] == "running"
    assert runner.followed_mode == mode
    assert tools.ledger.get("r1")["interaction_mode"] == mode
    tools.ledger.close()


def test_followup_timeout_on_terminal_request_reopens_as_unknown(make):
    tools, runner, ledger = make()
    _submit(tools)
    ledger.update_state("r1", "succeeded", NOW)
    before = ledger.open_count()

    def slow_followup(request_id, prompt, interaction_mode="default"):
        raise FleetTimeout("followup")

    runner.followup = slow_followup
    out = tools.call_tool("t3_followup", {"request_id": "r1", "message": "one more change"})
    assert out["error"]["code"] == "submit-unknown"
    assert out["error"]["request_id"] == "r1"
    assert ledger.get("r1")["last_state"] == "unknown"
    assert ledger.open_count() == before + 1


def test_prefixed_title_is_capped_for_t3_fleet(make):
    tools, runner, _ = make()
    _submit(tools, title="x" * 120, caller="A" * 48)
    sent = runner.submits[0]["title"]
    assert len(sent) <= 120
    assert sent.startswith("[grokbot:" + "A" * 48 + "] ")


def test_timed_out_submit_reserves_an_open_slot_until_expiry(make):
    from datetime import timedelta

    from brigade.grokbot_t3.ledger import IN_DOUBT_TTL

    tools, runner, ledger = make(max_open=1)
    runner.submit_errors = [FleetTimeout("submit")]
    assert _submit(tools)["error"]["code"] == "submit-unknown"
    assert ledger.open_count(NOW) == 1
    assert _submit(tools)["error"]["code"] == "open-limit"
    assert len(runner.submits) == 1
    assert ledger.open_count(NOW + IN_DOUBT_TTL + timedelta(seconds=1)) == 0


def test_unreadable_submit_without_id_reserves_an_open_slot(make):
    from brigade.grokbot_t3.fleet import FleetProtocolError

    tools, runner, ledger = make(max_open=1)
    runner.submit_errors = [FleetProtocolError("internal_error")]
    assert _submit(tools)["error"]["code"] == "submit-unknown"
    assert _submit(tools)["error"]["code"] == "open-limit"


def test_success_envelope_without_request_id_reserves_a_slot(make, monkeypatch):
    tools, runner, ledger = make(max_open=1)
    monkeypatch.setattr(runner, "submit", lambda **kwargs: {"state": "running", "host": kwargs["host"]})
    assert _submit(tools)["error"]["code"] == "submit-unknown"
    assert ledger.open_count(NOW) == 1
    assert _submit(tools)["error"]["code"] == "open-limit"


def test_slot_is_reserved_while_t3_fleet_submits(make, monkeypatch):
    tools, runner, ledger = make()
    seen = []
    original = runner.submit

    def watching_submit(**kwargs):
        seen.append(ledger.open_count(NOW))
        return original(**kwargs)

    monkeypatch.setattr(runner, "submit", watching_submit)
    assert _submit(tools)["request_id"] == "r1"
    assert seen == [1]
    assert ledger.open_count(NOW) == 1


def test_definite_refusal_releases_the_reservation(make):
    tools, runner, ledger = make()
    runner.submit_errors = [FleetCallError("invalid_request")]
    assert _submit(tools)["error"]["code"] == "fleet-error"
    assert ledger.open_count(NOW) == 0


def test_stateless_followup_on_terminal_request_becomes_unknown(make, monkeypatch):
    tools, runner, ledger = make()
    _submit(tools)
    ledger.update_state("r1", "succeeded", NOW)
    monkeypatch.setattr(
        runner, "followup", lambda request_id, prompt, interaction_mode="default": {"request_id": request_id}
    )
    out = tools.call_tool("t3_followup", {"request_id": "r1", "message": "again"})
    assert out["error"]["code"] == "submit-unknown"
    assert ledger.get("r1")["last_state"] == "unknown"


def test_daily_limit_on_fallback_releases_the_reservation(make):
    tools, runner, ledger = make(daily_limit=1)
    runner.submit_errors = [FleetCallError("policy_rejected", "model-admission-denied")]
    assert _submit(tools)["error"]["code"] == "daily-limit"
    assert ledger.open_count(NOW) == 0


def test_terminal_followup_is_open_while_fleet_runs(make, monkeypatch):
    tools, runner, ledger = make()
    _submit(tools)
    ledger.update_state("r1", "succeeded", NOW)
    ledger.mark_notified("r1", "succeeded", NOW)
    seen = []

    def watching_followup(request_id, prompt, interaction_mode="default"):
        seen.append(ledger.get(request_id)["last_state"])
        raise FleetCallError("invalid_request")

    monkeypatch.setattr(runner, "followup", watching_followup)
    out = tools.call_tool("t3_followup", {"request_id": "r1", "message": "again"})
    assert seen == ["reopening"]
    assert out["error"]["code"] == "fleet-error"
    row = ledger.get("r1")
    assert row["last_state"] == "succeeded"
    assert row["notified_state"] == "succeeded"


def test_status_does_not_overwrite_a_newer_state(make, monkeypatch):
    tools, runner, ledger = make()
    _submit(tools)

    def racing_status(request_id):
        ledger.update_state(request_id, "needs_attention", NOW)
        return {"request_id": request_id, "state": "succeeded"}

    monkeypatch.setattr(runner, "status", racing_status)
    tools.call_tool("t3_status", {"request_id": "r1"})
    assert ledger.get("r1")["last_state"] == "needs_attention"


def test_status_during_followup_cannot_clobber_the_reopen(make, monkeypatch):
    tools, runner, ledger = make()
    _submit(tools)
    ledger.update_state("r1", "succeeded", NOW)

    def racing_followup(request_id, prompt, interaction_mode="default"):
        # A direct status call lands while t3-fleet is accepting the follow-up and still reports the old turn.
        monkeypatch.setattr(runner, "status", lambda rid: {"request_id": rid, "state": "succeeded"})
        tools.call_tool("t3_status", {"request_id": request_id})
        assert ledger.get(request_id)["last_state"] == "reopening"
        return {"request_id": request_id, "state": "running"}

    monkeypatch.setattr(runner, "followup", racing_followup)
    out = tools.call_tool("t3_followup", {"request_id": "r1", "message": "again"})
    assert out["state"] == "running"
    assert ledger.get("r1")["last_state"] == "running"
    assert ledger.open_count(NOW) == 1


def test_status_during_running_followup_cannot_clobber_the_new_turn(make, monkeypatch):
    tools, runner, ledger = make()
    _submit(tools)
    ledger.update_state("r1", "running", NOW)

    def racing_followup(request_id, prompt, interaction_mode="default"):
        # The previous turn finishes and a status call records it while t3-fleet is accepting the follow-up.
        monkeypatch.setattr(runner, "status", lambda rid: {"request_id": rid, "state": "succeeded"})
        tools.call_tool("t3_status", {"request_id": request_id})
        assert ledger.get(request_id)["last_state"] == "reopening"
        return {"request_id": request_id, "state": "running"}

    monkeypatch.setattr(runner, "followup", racing_followup)
    out = tools.call_tool("t3_followup", {"request_id": "r1", "message": "again"})
    assert out["state"] == "running"
    assert ledger.get("r1")["last_state"] == "running"
    assert ledger.open_count(NOW) == 1


def test_refused_running_followup_restores_the_prior_state(make, monkeypatch):
    tools, runner, ledger = make()
    _submit(tools)
    ledger.update_state("r1", "needs_attention", NOW)
    ledger.mark_notified("r1", "needs_attention", NOW)

    def refused(request_id, prompt, interaction_mode="default"):
        raise FleetCallError("invalid_request")

    monkeypatch.setattr(runner, "followup", refused)
    out = tools.call_tool("t3_followup", {"request_id": "r1", "message": "again"})
    assert out["error"]["code"] == "fleet-error"
    row = ledger.get("r1")
    assert row["last_state"] == "needs_attention"
    assert row["notified_state"] == "needs_attention"


def test_stale_reopening_marker_can_be_replaced_by_pollers(make):
    from datetime import timedelta

    from brigade.grokbot_t3 import contracts as c

    tools, runner, ledger = make()
    _submit(tools)
    ledger.update_state("r1", c.REOPENING_STATE, NOW, owner_write=True)
    assert not ledger.update_state("r1", "succeeded", NOW + timedelta(seconds=60))
    assert ledger.update_state("r1", "succeeded", NOW + timedelta(seconds=c.REOPENING_TTL_SECONDS + 1))
    assert ledger.get("r1")["last_state"] == "succeeded"


def _poll_before_reopen(ledger, monkeypatch, times):
    """Make a concurrent poll rewrite the same terminal state just before each reopen compare-and-set."""
    from brigade.grokbot_t3 import contracts as c

    original = ledger.update_state
    left = [times]

    def racing_update(request_id, state, now, **kwargs):
        if state == c.REOPENING_STATE and left[0]:
            left[0] -= 1
            original(request_id, "succeeded", now)
        return original(request_id, state, now, **kwargs)

    monkeypatch.setattr(ledger, "update_state", racing_update)


def test_followup_retries_the_reopen_marker_after_a_racing_poll(make, monkeypatch):
    tools, runner, ledger = make()
    _submit(tools)
    ledger.update_state("r1", "succeeded", NOW)
    _poll_before_reopen(ledger, monkeypatch, times=1)
    seen = []

    def watching_followup(request_id, prompt, interaction_mode="default"):
        seen.append(ledger.get(request_id)["last_state"])
        return {"request_id": request_id, "state": "running"}

    monkeypatch.setattr(runner, "followup", watching_followup)
    out = tools.call_tool("t3_followup", {"request_id": "r1", "message": "again"})
    assert seen == ["reopening"]
    assert out["state"] == "running"
    assert ledger.get("r1")["last_state"] == "running"


def test_followup_does_not_launch_without_the_reopen_marker(make, monkeypatch):
    tools, runner, ledger = make()
    _submit(tools)
    ledger.update_state("r1", "succeeded", NOW)
    _poll_before_reopen(ledger, monkeypatch, times=100)
    launched = []
    monkeypatch.setattr(
        runner, "followup", lambda request_id, prompt, interaction_mode="default": launched.append(request_id)
    )
    out = tools.call_tool("t3_followup", {"request_id": "r1", "message": "again"})
    assert out["error"]["code"] == "unavailable"
    assert launched == []
    assert ledger.get("r1")["last_state"] == "succeeded"


@pytest.mark.parametrize("flag", [None, False])
def test_disabled_messaging_excludes_hub_without_constructing_client(make, monkeypatch, tmp_path, flag):
    from brigade.grokbot_t3 import tools as module

    def forbidden(**kwargs):
        pytest.fail("disabled messaging constructed a Hub client")

    monkeypatch.setattr(module, "HubInbox", forbidden)
    options = {"hub_messaging_enabled": flag} if flag is not None else {}
    tools, _, _ = make(
        hub_token_file=str(tmp_path / "principal.token"), hub_message_principal="agent:grokbot", **options
    )
    assert len(tools.inventory()) == 9
    for name, arguments in (
        ("hub_inbox_list", {}),
        ("hub_inbox_ack", {"ids": [1]}),
        ("hub_message_send", {"alias": "host-a", "subject": "s", "body": "b"}),
        ("hub_message_status", {"ids": [1]}),
    ):
        assert tools.call_tool(name, arguments)["error"]["code"] == "hub-not-configured"


@pytest.mark.parametrize(
    "alias",
    ["Host-a", "host", "host-a/child", "../host-a", "00000000-0000-4000-8000-000000000001", "agent:host-a", "unknown"],
)
def test_invalid_alias_never_constructs_client_or_uses_clock_or_dispatch_budget(make, monkeypatch, tmp_path, alias):
    from brigade.grokbot_t3 import tools as module

    def forbidden(*args, **kwargs):
        pytest.fail("invalid alias reached a side effect")

    monkeypatch.setattr(module, "HubInbox", forbidden)
    tools, _, ledger = make(
        hub_messaging_enabled=True,
        hub_message_principal="agent:grokbot",
        hub_token_file=str(tmp_path / "principal.token"),
        hub_recipients={"host-a": "00000000-0000-4000-8000-000000000001"},
    )
    monkeypatch.setattr(tools, "now", forbidden)
    monkeypatch.setattr(ledger, "record_attempt", forbidden)
    result = tools.call_tool("hub_message_send", {"alias": alias, "subject": "s", "body": "b"})
    assert result["error"]["code"] == "invalid_request"
