"""Poisoned spool batches isolate rejected events without dropping peers (#1735, #1710)."""

from __future__ import annotations

import json
import threading
from pathlib import Path

import pytest

from brigade import fleet_client, fleet_client_flush

NODE = "11111111-1111-4111-8111-111111111111"
HUB = "https://hub.invalid"


def _event(seq: int, *, poison: bool = False) -> dict:
    event = {
        "node_id": NODE,
        "run_id": "r1",
        "repo": "repo-a",
        "seat": "worker",
        "harness": "claude",
        "state": "run.created",
        "ts": f"2026-08-23T12:00:{seq:02d}+00:00",
        "sequence": seq,
        "digest": f"d{seq}",
    }
    if poison:
        event["repo"] = "x" * 2048
    return event


class FakeHub:
    """Rejects a whole batch atomically when any event in it is poisoned."""

    def __init__(self, *, fail_on_call: int | None = None, reason: str = "hub rejected batch with HTTP 422"):
        self.calls: list[list[int]] = []
        self.stored: list[int] = []
        self.fail_on_call = fail_on_call
        self.reason = reason

    def post(self, hub_url, token, body, *, timeout):
        batch = body if isinstance(body, list) else [body]
        self.calls.append([e["sequence"] for e in batch])
        if self.fail_on_call is not None and len(self.calls) == self.fail_on_call:
            raise TimeoutError("hub unreachable")
        if any(len(e["repo"]) > 1024 for e in batch):
            raise fleet_client._PoisonResponse(self.reason)
        for event in batch:
            if event["sequence"] not in self.stored:
                self.stored.append(event["sequence"])


@pytest.fixture(autouse=True)
def _home(tmp_path, monkeypatch):
    monkeypatch.setenv("BRIGADE_HOME", str(tmp_path / "brigade-home"))


def _spool(events: list[dict]) -> Path:
    path = fleet_client.spool_path(NODE)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(e, sort_keys=True) + "\n" for e in events))
    return path


def _flush(hub: FakeHub, monkeypatch, **kwargs) -> int:
    monkeypatch.setattr(fleet_client, "_post_events", hub.post)
    path = fleet_client.spool_path(NODE)
    with fleet_client._spool_lock(path) as dir_fd:
        return fleet_client._flush_locked(
            path, HUB, "token", max_batches=kwargs.pop("max_batches", None), timeout=1.0, dir_fd=dir_fd, **kwargs
        )


def _sequences(path: Path) -> list[int]:
    if not path.exists():
        return []
    return [json.loads(line)["sequence"] for line in path.read_text().splitlines() if line.strip()]


def _dead_letters(path: Path) -> list[dict]:
    dead = fleet_client_flush.dead_letter_path(path)
    if not dead.exists():
        return []
    return [json.loads(line) for line in dead.read_text().splitlines() if line.strip()]


@pytest.mark.parametrize("poison_at", [0, 2, 4])
def test_one_poisoned_event_isolated_at_head_middle_and_tail(monkeypatch, poison_at):
    events = [_event(seq, poison=seq - 1 == poison_at) for seq in range(1, 6)]
    path = _spool(events)
    hub = FakeHub()
    assert _flush(hub, monkeypatch) == 4
    valid = [seq for seq in range(1, 6) if seq - 1 != poison_at]
    assert hub.stored == valid
    assert not path.exists()
    [record] = _dead_letters(path)
    assert record["sequence"] == poison_at + 1
    assert record["digest"] == f"d{poison_at + 1}"
    assert record["ts"] == events[poison_at]["ts"]
    assert record["event"] == events[poison_at]
    assert record["reason"] == "hub rejected batch with HTTP 422"


def test_multiple_poisoned_events_keep_valid_order(monkeypatch):
    events = [_event(seq, poison=seq in (2, 3, 7)) for seq in range(1, 9)]
    path = _spool(events)
    hub = FakeHub()
    assert _flush(hub, monkeypatch) == 5
    assert hub.stored == [1, 4, 5, 6, 8]
    assert [r["sequence"] for r in _dead_letters(path)] == [2, 3, 7]
    assert not path.exists()


def test_transport_failure_during_bisection_keeps_unprocessed_events(monkeypatch):
    # Call 1 rejects [1..6]; call 2 delivers [1, 2, 3]; call 3 (the [4, 5, 6]
    # half) fails in transit, so 4, 5 and 6 stay queued, poison included.
    events = [_event(seq, poison=seq == 5) for seq in range(1, 7)]
    path = _spool(events)
    hub = FakeHub(fail_on_call=3)
    assert _flush(hub, monkeypatch) == 3
    assert hub.stored == [1, 2, 3]
    assert _sequences(path) == [4, 5, 6]
    assert _dead_letters(path) == []


def test_transport_failure_after_isolation_keeps_dead_letter_and_suffix(monkeypatch):
    # [1..4] rejected, [1, 2] rejected, [1] rejected (isolated), [2] fails.
    events = [_event(seq, poison=seq == 1) for seq in range(1, 5)]
    path = _spool(events)
    hub = FakeHub(fail_on_call=4)
    assert _flush(hub, monkeypatch) == 0
    assert hub.stored == []
    assert _sequences(path) == [2, 3, 4]
    assert [r["sequence"] for r in _dead_letters(path)] == [1]


def test_interrupted_cleanup_replays_idempotently(monkeypatch):
    events = [_event(seq, poison=seq == 2) for seq in range(1, 4)]
    path = _spool(events)
    hub = FakeHub()
    real_write = fleet_client._write_spool_atomic

    def crash_on_spool_rewrite(target, records, *, dir_fd=None):
        if target == path:
            raise OSError("disk full")
        return real_write(target, records, dir_fd=dir_fd)

    monkeypatch.setattr(fleet_client, "_write_spool_atomic", crash_on_spool_rewrite)
    with pytest.raises(OSError):
        _flush(hub, monkeypatch)
    assert _sequences(path) == [1, 2, 3]
    monkeypatch.setattr(fleet_client, "_write_spool_atomic", real_write)
    assert _flush(hub, monkeypatch) == 2
    assert hub.stored == [1, 3]
    assert not path.exists()
    assert [r["sequence"] for r in _dead_letters(path)] == [2]


def test_dead_letter_write_failure_keeps_poison_queued(monkeypatch):
    events = [_event(seq, poison=seq == 2) for seq in range(1, 4)]
    path = _spool(events)
    hub = FakeHub()
    real_write = fleet_client._write_spool_atomic
    dead = fleet_client_flush.dead_letter_path(path)

    def refuse_dead_letter(target, records, *, dir_fd=None):
        if target == dead:
            raise OSError("read-only")
        return real_write(target, records, dir_fd=dir_fd)

    monkeypatch.setattr(fleet_client, "_write_spool_atomic", refuse_dead_letter)
    assert _flush(hub, monkeypatch) == 2
    assert hub.stored == [1, 3]
    assert _sequences(path) == [2]


def test_append_during_flush_is_preserved(monkeypatch):
    events = [_event(seq, poison=seq == 1) for seq in range(1, 4)]
    path = _spool(events)
    hub = FakeHub()
    appender: list[threading.Thread] = []
    real_post = hub.post

    def post_and_race_an_append(hub_url, token, body, *, timeout):
        if not appender:

            def append() -> None:
                with fleet_client._spool_lock(path) as dir_fd:
                    fleet_client._spool_append_locked(path, _event(99), dir_fd=dir_fd)

            appender.append(threading.Thread(target=append))
            appender[0].start()
        return real_post(hub_url, token, body, timeout=timeout)

    monkeypatch.setattr(hub, "post", post_and_race_an_append)
    assert _flush(hub, monkeypatch) == 2
    appender[0].join(10)
    assert hub.stored == [2, 3]
    assert _sequences(path) == [99]


def test_dead_letter_is_bounded_and_deduplicated(monkeypatch):
    monkeypatch.setattr(fleet_client_flush, "DEAD_LETTER_MAX_RECORDS", 3)
    path = _spool([_event(seq, poison=True) for seq in range(1, 6)])
    assert _flush(FakeHub(), monkeypatch) == 0
    assert [r["sequence"] for r in _dead_letters(path)] == [3, 4, 5]
    _spool([_event(5, poison=True)])
    assert _flush(FakeHub(), monkeypatch) == 0
    assert [r["sequence"] for r in _dead_letters(path)] == [3, 4, 5]


def test_oversized_dead_letter_keeps_identity_without_body(monkeypatch):
    monkeypatch.setattr(fleet_client_flush, "DEAD_LETTER_MAX_EVENT_BYTES", 512)
    path = _spool([_event(1, poison=True)])
    assert _flush(FakeHub(), monkeypatch) == 0
    [record] = _dead_letters(path)
    assert record["sequence"] == 1 and record["digest"] == "d1" and record["ts"] == _event(1)["ts"]
    assert "event" not in record and record["event_omitted"] is True


def test_rejection_reason_is_sanitized(monkeypatch):
    path = _spool([_event(1, poison=True)])
    assert _flush(FakeHub(reason="bad\x1b[31m\nreason" + "y" * 500), monkeypatch) == 0
    [record] = _dead_letters(path)
    assert record["reason"].startswith("bad?[31m?reason")
    assert len(record["reason"]) <= fleet_client_flush.REASON_MAX_CHARS


def test_preserve_poison_still_stops_without_bisecting(monkeypatch):
    path = _spool([_event(seq, poison=seq == 2) for seq in range(1, 4)])
    hub = FakeHub()
    assert _flush(hub, monkeypatch, preserve_poison=True) == 0
    assert hub.calls == [[1, 2, 3]]
    assert _sequences(path) == [1, 2, 3]


def test_report_path_bounds_poison_isolation_to_its_budget(tmp_path, monkeypatch):
    """Codex P1 on #1831: report_event keeps its bounded latency while isolating poison."""
    from brigade import node as node_mod

    home = tmp_path / "brigade-home"
    identity = node_mod.NodeIdentity(node_id=NODE, hostname="fleet-test", roles=(), platform="test")
    node_mod.node_path(home).parent.mkdir(parents=True, exist_ok=True)
    node_mod.node_path(home).write_text(node_mod._format_node_toml(identity), encoding="utf-8")
    monkeypatch.setenv("BRIGADE_FLEET_HUB_URL", HUB)
    monkeypatch.setenv("BRIGADE_FLEET_TOKEN", "token")
    path = _spool([_event(seq, poison=True) for seq in range(1, 9)])
    now = [0.0]
    hub = FakeHub()
    real_post = hub.post

    def slow_post(hub_url, token, body, *, timeout):
        assert timeout > 0
        now[0] += 1.0
        return real_post(hub_url, token, body, timeout=timeout)

    monkeypatch.setattr(fleet_client_flush, "_monotonic", lambda: now[0])
    monkeypatch.setattr(fleet_client, "_post_events", slow_post)
    fleet_client.report_event(_event(9))
    # One batch post plus at most REPORT_TIMEOUT_SECONDS of one-second bisection posts.
    assert len(hub.calls) <= 1 + int(fleet_client.REPORT_TIMEOUT_SECONDS)
    assert now[0] <= 2 * fleet_client.REPORT_TIMEOUT_SECONDS
    assert len(_sequences(path)) + len(_dead_letters(path)) == 9


def test_explicit_flush_bisects_without_a_budget(monkeypatch):
    now = [0.0]
    hub = FakeHub()
    real_post = hub.post

    def slow_post(hub_url, token, body, *, timeout):
        now[0] += 10.0
        return real_post(hub_url, token, body, timeout=timeout)

    monkeypatch.setattr(fleet_client_flush, "_monotonic", lambda: now[0])
    path = _spool([_event(seq, poison=seq == 4) for seq in range(1, 9)])
    monkeypatch.setattr(hub, "post", slow_post)
    assert _flush(hub, monkeypatch) == 7
    assert [r["sequence"] for r in _dead_letters(path)] == [4]
