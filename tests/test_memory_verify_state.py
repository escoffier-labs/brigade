"""The verify job's state file fails closed and never echoes its contents."""

from __future__ import annotations

import json
import os

import pytest

from brigade import cli, memory_hub_client as hubc, memory_verify_state as vstate

pytestmark = pytest.mark.skipif(
    os.open not in os.supports_dir_fd or not hasattr(os, "O_NOFOLLOW"),
    reason="safe directory descriptors unavailable",
)

SECRET = "Zanzibarquill"
KEY = "0" * 32


def state_path(target):
    return target / vstate.STATE


def write_raw(target, data: bytes, mode=0o600):
    path = state_path(target)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    path.write_bytes(data)
    os.chmod(path, mode)
    return path


def valid_state():
    state = vstate.fresh()
    state["scopes"] = {
        "repo:example": {
            "since_day": 20300,
            "never_read": 2,
            "cards": {
                KEY: {
                    "seen": 1,
                    "proposed": 0,
                    "verified": 20390,
                    "revision": 4,
                    "evidence": "a" * 64,
                    "verdict": "supported",
                    "pending": hubc.event_id(),
                    "reads": [1, 2, 3],
                }
            },
        }
    }
    return state


def assert_fixed(exc_info, reason=vstate.STATE_ERROR):
    assert str(exc_info.value) == reason
    assert SECRET not in str(exc_info.value)


def test_absent_state_is_fresh_and_round_trips_owner_only(tmp_path):
    assert vstate.load(tmp_path) == vstate.fresh()
    state = valid_state()
    vstate.save(tmp_path, state)
    assert vstate.load(tmp_path) == state
    assert os.stat(state_path(tmp_path)).st_mode & 0o777 == 0o600
    assert os.stat(state_path(tmp_path).parent).st_mode & 0o777 == 0o700
    assert [p.name for p in state_path(tmp_path).parent.iterdir()] == ["state.json"]


def test_oversized_state_fails_closed_without_text(tmp_path):
    padding = SECRET * (vstate.MAX_BYTES // len(SECRET) + 1)
    write_raw(tmp_path, json.dumps({**vstate.fresh(), "pad": padding}).encode())
    with pytest.raises(vstate.VerifyStateError) as exc_info:
        vstate.load(tmp_path)
    assert_fixed(exc_info)


@pytest.mark.parametrize(
    "raw",
    [
        b"{not json " + SECRET.encode(),
        b"\xff\xfe" + SECRET.encode(),
        json.dumps([SECRET]).encode(),
        json.dumps({**vstate.fresh(), "schema": SECRET}).encode(),
        json.dumps({**vstate.fresh(), "note": SECRET}).encode(),
        json.dumps({**vstate.fresh(), "consecutive_failures": 3, "paused": False}).encode(),
        json.dumps({**vstate.fresh(), "consecutive_failures": -1}).encode(),
        json.dumps({**vstate.fresh(), "last_status": SECRET}).encode(),
        json.dumps({**vstate.fresh(), "scopes": {SECRET: {}}}).encode(),
        json.dumps({**vstate.fresh(), "scopes": {"fleet": {"cards": {SECRET: {"seen": 1, "proposed": 0}}}}}).encode(),
        json.dumps({**vstate.fresh(), "scopes": {"fleet": {"cards": {KEY: {"seen": 1}}}}}).encode(),
        json.dumps(
            {**vstate.fresh(), "scopes": {"fleet": {"cards": {KEY: {"seen": 1, "proposed": 0, "text": SECRET}}}}}
        ).encode(),
        json.dumps(
            {**vstate.fresh(), "scopes": {"fleet": {"cards": {KEY: {"seen": 1, "proposed": 0, "verdict": SECRET}}}}}
        ).encode(),
        json.dumps(
            {**vstate.fresh(), "scopes": {"fleet": {"cards": {KEY: {"seen": 1, "proposed": 0, "reads": [1, 2]}}}}}
        ).encode(),
        json.dumps({**vstate.fresh(), "scopes": {"fleet": {"since_day": SECRET}}}).encode(),
    ],
)
def test_malformed_state_fails_closed_with_a_fixed_reason(tmp_path, raw):
    write_raw(tmp_path, raw)
    with pytest.raises(vstate.VerifyStateError) as exc_info:
        vstate.load(tmp_path)
    assert_fixed(exc_info)


def test_group_readable_state_is_unsafe(tmp_path):
    write_raw(tmp_path, json.dumps(vstate.fresh()).encode(), mode=0o644)
    with pytest.raises(vstate.VerifyStateError) as exc_info:
        vstate.load(tmp_path)
    assert_fixed(exc_info)


def test_symlinked_state_is_unsafe(tmp_path):
    elsewhere = tmp_path / "elsewhere.json"
    elsewhere.write_text(json.dumps(vstate.fresh()))
    os.chmod(elsewhere, 0o600)
    state_path(tmp_path).parent.mkdir(parents=True, mode=0o700)
    state_path(tmp_path).symlink_to(elsewhere)
    with pytest.raises(vstate.VerifyStateError) as exc_info:
        vstate.load(tmp_path)
    assert_fixed(exc_info)


def test_hard_linked_state_is_unsafe(tmp_path):
    path = write_raw(tmp_path, json.dumps(vstate.fresh()).encode())
    os.link(path, tmp_path / "second-name.json")
    with pytest.raises(vstate.VerifyStateError) as exc_info:
        vstate.load(tmp_path)
    assert_fixed(exc_info)


def test_saving_an_oversized_or_invalid_state_keeps_the_old_file(tmp_path):
    vstate.save(tmp_path, valid_state())
    before = state_path(tmp_path).read_bytes()
    huge = vstate.fresh()
    cards = {f"{index:032x}": {"seen": 10**15, "proposed": 10**15, "reads": [10**9] * 3} for index in range(50_000)}
    huge["scopes"] = {f"repo:s{index}": {"cards": cards} for index in range(2)}
    with pytest.raises(vstate.VerifyStateError) as exc_info:
        vstate.save(tmp_path, huge)
    assert_fixed(exc_info)
    with pytest.raises(vstate.VerifyStateError):
        vstate.save(tmp_path, {**vstate.fresh(), "note": SECRET})
    assert state_path(tmp_path).read_bytes() == before
    assert [p.name for p in state_path(tmp_path).parent.iterdir()] == ["state.json"]


def test_three_failures_pause_and_one_success_clears():
    state = vstate.fresh()
    for count in (1, 2, 3, 4):
        vstate.record_run(state, "failed")
        assert state["consecutive_failures"] == count
        assert state["paused"] == (count >= vstate.FAILURES)
        vstate.validate(state)
    vstate.record_run(state, "completed")
    assert state["consecutive_failures"] == 0 and state["paused"] is False
    assert state["last_status"] == "completed"


def test_the_lock_refuses_a_second_holder(tmp_path):
    with vstate.locked(tmp_path):
        with pytest.raises(vstate.VerifyStateError) as exc_info:
            with vstate.locked(tmp_path, deadline_seconds=0.05):
                pass
        assert str(exc_info.value) == vstate.LOCK_ERROR
    with vstate.locked(tmp_path):
        pass


def test_status_reports_a_bad_state_as_paused_with_only_the_fixed_reason(tmp_path, capsys):
    write_raw(tmp_path, b"{" + SECRET.encode())
    assert vstate.summary(tmp_path) == {"paused": True, "state_error": vstate.STATE_ERROR}
    code = cli.main(["memory", "verify", "status", "--target", str(tmp_path), "--json"])
    out = capsys.readouterr().out
    assert code == 0
    assert json.loads(out)["state"] == {"paused": True, "state_error": vstate.STATE_ERROR}
    assert SECRET not in out


def test_status_summary_holds_counts_only(tmp_path):
    vstate.save(tmp_path, valid_state())
    assert vstate.summary(tmp_path) == {
        "consecutive_failures": 0,
        "paused": False,
        "never_read": {"repo:example": 2},
        "tracked_cards": 1,
    }
