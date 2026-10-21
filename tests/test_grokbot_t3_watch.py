"""t3-dispatch wake watcher."""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone

import pytest

from brigade.grokbot_t3 import contracts as c
from brigade.grokbot_t3 import watch
from brigade.grokbot_t3.fleet import FleetTimeout
from brigade.grokbot_t3.ledger import Ledger

NOW = datetime(2026, 10, 16, 12, 0, tzinfo=timezone.utc)


def _owner_file(path, text):
    path.write_text(text, encoding="utf-8")
    os.chmod(path, 0o600)
    return path


@pytest.fixture
def setup(tmp_path, t3_ledger_storage, t3_owner_reads, t3_watch_log):
    key = _owner_file(tmp_path / "builder.key", "wake-key-value\n")
    wake = _owner_file(
        tmp_path / "wake.json",
        json.dumps(
            {
                "schema": watch.WAKE_SCHEMA,
                "targets": {
                    "Builder": [{"url": "https://api2.cursor.sh/automations/webhook/abc", "key_file": str(key)}],
                    "default": [{"url": "https://api2.cursor.sh/automations/webhook/def", "key_file": str(key)}],
                },
            }
        ),
    )
    ledger = Ledger(tmp_path / "ledger.sqlite3")
    return tmp_path, watch.load_wake_config(wake), ledger


class Runner:
    def __init__(self, states):
        self.states = states

    def status(self, request_id):
        value = self.states[request_id]
        if isinstance(value, Exception):
            raise value
        return {"schema": "t3-fleet.result.v1", "request_id": request_id, "state": value}


class Post:
    def __init__(self, codes):
        self.codes = list(codes)
        self.calls = []

    def __call__(self, url, key, body):
        self.calls.append((url, key, body))
        return self.codes.pop(0) if self.codes else 200


def _record(ledger, request_id, caller="Builder", state="running"):
    ledger.record_submit(
        request_id=request_id,
        caller=caller,
        repository="brigade",
        host="host-a",
        seat="dsflash",
        title="[grokbot:Builder] t",
        prompt_sha256="a" * 64,
        state=state,
        now=NOW,
    )


def test_wakes_on_terminal_state_once(setup):
    tmp_path, targets, ledger = setup
    _record(ledger, "r1")
    post = Post([200])
    summary = watch.run_watch_once(
        runner=Runner({"r1": "succeeded"}),
        ledger=ledger,
        wake_targets=targets,
        log_path=tmp_path / "wake-notify.jsonl",
        now=lambda: NOW,
        post=post,
    )
    assert summary == {"checked": 1, "woken": 1, "failed": 0}
    url, key, body = post.calls[0]
    assert url.endswith("/abc") and key == "wake-key-value"
    assert body == {"request_id": "r1", "state": "succeeded", "repository": "brigade", "title": "[grokbot:Builder] t"}
    again = watch.run_watch_once(
        runner=Runner({"r1": "succeeded"}),
        ledger=ledger,
        wake_targets=targets,
        log_path=tmp_path / "wake-notify.jsonl",
        now=lambda: NOW,
        post=post,
    )
    assert again == {"checked": 0, "woken": 0, "failed": 0}
    log = (tmp_path / "wake-notify.jsonl").read_text().splitlines()
    assert json.loads(log[0])["statuses"] == [200]


def test_no_wake_while_running(setup):
    tmp_path, targets, ledger = setup
    _record(ledger, "r1")
    post = Post([])
    summary = watch.run_watch_once(
        runner=Runner({"r1": "running"}),
        ledger=ledger,
        wake_targets=targets,
        log_path=tmp_path / "w.jsonl",
        now=lambda: NOW,
        post=post,
    )
    assert summary == {"checked": 1, "woken": 0, "failed": 0}
    assert post.calls == []


def test_retry_after_non_200(setup):
    tmp_path, targets, ledger = setup
    _record(ledger, "r1")
    post = Post([500, 200])
    kwargs = dict(
        runner=Runner({"r1": "failed"}),
        ledger=ledger,
        wake_targets=targets,
        log_path=tmp_path / "w.jsonl",
        now=lambda: NOW,
        post=post,
    )
    assert watch.run_watch_once(**kwargs)["failed"] == 1
    assert watch.run_watch_once(**kwargs)["woken"] == 1
    assert ledger.get("r1")["notified_state"] == "failed"


def test_second_question_wakes_again(setup):
    tmp_path, targets, ledger = setup
    _record(ledger, "r1")
    post = Post([])
    states = {"r1": "needs_attention"}
    kwargs = dict(
        runner=Runner(states),
        ledger=ledger,
        wake_targets=targets,
        log_path=tmp_path / "w.jsonl",
        now=lambda: NOW,
        post=post,
    )
    watch.run_watch_once(**kwargs)
    states["r1"] = "running"
    watch.run_watch_once(**kwargs)
    states["r1"] = "needs_attention"
    watch.run_watch_once(**kwargs)
    assert [call[2]["state"] for call in post.calls] == ["needs_attention", "needs_attention"]


def test_unknown_caller_uses_default_and_status_failure_counts(setup):
    tmp_path, targets, ledger = setup
    _record(ledger, "r1", caller="Stranger")
    _record(ledger, "r2")
    post = Post([])
    summary = watch.run_watch_once(
        runner=Runner({"r1": "succeeded", "r2": FleetTimeout("status")}),
        ledger=ledger,
        wake_targets=targets,
        log_path=tmp_path / "w.jsonl",
        now=lambda: NOW,
        post=post,
    )
    assert post.calls[0][0].endswith("/def")
    assert summary == {"checked": 2, "woken": 1, "failed": 1}


@pytest.mark.parametrize(
    "targets",
    [
        {"Builder": [{"url": "http://insecure.example/x", "key_file": "/k"}]},
        {"Builder": [{"url": "https://ok.example/x", "key_file": "relative"}]},
        {"bad name!": [{"url": "https://ok.example/x", "key_file": "/k"}]},
        {"Builder": []},
    ],
)
def test_rejects_bad_wake_config(tmp_path, targets, t3_owner_reads):
    # Keep the default valid so each case still reaches its own invalid field.
    targets = {"default": [{"url": "https://example.com/wake", "key_file": str(tmp_path / "wake.key")}], **targets}
    path = _owner_file(tmp_path / "wake.json", json.dumps({"schema": watch.WAKE_SCHEMA, "targets": targets}))
    with pytest.raises(c.T3DispatchError):
        watch.load_wake_config(path)


def test_post_wake_refuses_redirect_and_proxy(monkeypatch):
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
    from threading import Thread

    seen = []

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            seen.append((self.command, self.path))
            self.send_response(302)
            self.send_header("Location", "/sink")
            self.end_headers()

        def do_GET(self):
            seen.append((self.command, self.path))
            self.send_response(200)
            self.end_headers()

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = Thread(target=server.serve_forever)
    thread.start()
    try:
        url = f"http://127.0.0.1:{server.server_port}/wake"
        assert watch.post_wake(url, "test-key", {"request_id": "r1"}) == 302
        assert seen == [("POST", "/wake")]
        # A configured proxy must not divert the authenticated request.
        monkeypatch.setenv("http_proxy", "http://127.0.0.1:1")
        monkeypatch.setenv("https_proxy", "http://127.0.0.1:1")
        monkeypatch.setenv("no_proxy", "")
        monkeypatch.setenv("NO_PROXY", "")
        assert watch.post_wake(url, "test-key", {}) == 302
        assert seen == [("POST", "/wake"), ("POST", "/wake")]
    finally:
        server.shutdown()
        thread.join()
        server.server_close()


def test_wake_config_requires_default_target(tmp_path, t3_owner_reads):
    path = _owner_file(
        tmp_path / "wake.json",
        json.dumps(
            {
                "schema": watch.WAKE_SCHEMA,
                "targets": {"Builder": [{"url": "https://example.com/wake", "key_file": str(tmp_path / "wake.key")}]},
            }
        ),
    )
    with pytest.raises(c.T3DispatchError):
        watch.load_wake_config(path)


def test_wake_gives_up_after_30_failures_across_restarts(setup):
    tmp_path, targets, ledger = setup
    _record(ledger, "r1", state="succeeded")
    post = Post([500] * 40)
    for index in range(35):
        if index == 15:
            ledger.close()
            ledger = Ledger(tmp_path / "ledger.sqlite3")
        watch.run_watch_once(
            runner=Runner({}),
            ledger=ledger,
            wake_targets=targets,
            log_path=tmp_path / "w.jsonl",
            now=lambda: NOW,
            post=post,
        )
    assert len(post.calls) == 30
    assert ledger.get("r1")["notified_state"] == "succeeded"
    assert json.loads((tmp_path / "w.jsonl").read_text().splitlines()[-1])["note"] == "gave-up"


def test_30_consecutive_status_failures_wake_unknown_once(setup):
    tmp_path, targets, ledger = setup
    _record(ledger, "r1")
    post = Post([])
    states = {"r1": FleetTimeout("status")}
    runner = Runner(states)
    kwargs = dict(
        runner=runner, ledger=ledger, wake_targets=targets, log_path=tmp_path / "w.jsonl", now=lambda: NOW, post=post
    )
    for _ in range(29):
        watch.run_watch_once(**kwargs)
    assert ledger.get("r1")["last_state"] == "running"
    states["r1"] = "running"
    watch.run_watch_once(**kwargs)  # A successful poll resets the consecutive failure count.
    states["r1"] = FleetTimeout("status")
    for _ in range(29):
        watch.run_watch_once(**kwargs)
    assert post.calls == []
    watch.run_watch_once(**kwargs)
    assert ledger.get("r1")["last_state"] == "unknown"
    assert [call[2]["state"] for call in post.calls] == ["unknown"]
    assert watch.run_watch_once(**kwargs) == {"checked": 0, "woken": 0, "failed": 0}
    assert len(post.calls) == 1


def test_multiple_targets_retry_only_failed_delivery_and_reset_on_state_change(setup):
    tmp_path, targets, ledger = setup
    targets["Builder"].append({**targets["Builder"][0], "url": "https://example.com/second"})
    _record(ledger, "r1")
    states = {"r1": "needs_attention"}
    post = Post([200, 500, 200, 200, 200])
    kwargs = dict(
        runner=Runner(states),
        ledger=ledger,
        wake_targets=targets,
        log_path=tmp_path / "w.jsonl",
        now=lambda: NOW,
        post=post,
    )
    watch.run_watch_once(**kwargs)
    ledger.close()
    kwargs["ledger"] = Ledger(tmp_path / "ledger.sqlite3")
    watch.run_watch_once(**kwargs)
    assert len(post.calls) == 3
    assert post.calls[1][0] == post.calls[2][0]
    states["r1"] = "running"
    watch.run_watch_once(**kwargs)
    states["r1"] = "needs_attention"
    watch.run_watch_once(**kwargs)
    assert len(post.calls) == 5
    assert post.calls[0][0] == post.calls[3][0]


def test_garbage_http_wake_is_bounded_and_later_rows_continue(setup):
    import socketserver
    from threading import Thread

    class Garbage(socketserver.BaseRequestHandler):
        def handle(self):
            self.request.recv(16384)
            self.request.sendall(b"GARBAGE\r\n")

    tmp_path, targets, ledger = setup
    _record(ledger, "bad", state="succeeded")
    _record(ledger, "later", caller="Other", state="succeeded")
    with socketserver.TCPServer(("127.0.0.1", 0), Garbage) as server:
        thread = Thread(target=server.serve_forever)
        thread.start()
        try:
            targets["Builder"][0]["url"] = f"http://127.0.0.1:{server.server_address[1]}/wake"
            assert watch.post_wake(targets["Builder"][0]["url"], "test-key", {}) == 0

            def post(url, key, body):
                return watch.post_wake(url, key, body) if body["request_id"] == "bad" else 200

            for _ in range(31):
                watch.run_watch_once(
                    runner=Runner({}),
                    ledger=ledger,
                    wake_targets=targets,
                    log_path=tmp_path / "w.jsonl",
                    now=lambda: NOW,
                    post=post,
                )
            assert ledger.get("bad")["wake_attempts"] == 30
            assert ledger.get("bad")["notified_state"] == "succeeded"
            assert ledger.get("later")["notified_state"] == "succeeded"
        finally:
            server.shutdown()
            thread.join()


def test_unexpected_row_failure_is_counted_and_later_rows_continue(setup):
    tmp_path, targets, ledger = setup
    _record(ledger, "bad", state="succeeded")
    _record(ledger, "later", state="succeeded")

    def post(url, key, body):
        if body["request_id"] == "bad":
            raise RuntimeError("unexpected transport failure")
        return 200

    summary = watch.run_watch_once(
        runner=Runner({}),
        ledger=ledger,
        wake_targets=targets,
        log_path=tmp_path / "w.jsonl",
        now=lambda: NOW,
        post=post,
    )
    assert summary == {"checked": 0, "woken": 1, "failed": 1}
    assert ledger.get("bad")["wake_attempts"] == 1
    assert ledger.get("later")["notified_state"] == "succeeded"


def test_exhausted_unknown_rows_recover_every_fifteenth_tick_across_restarts(setup):
    tmp_path, targets, ledger = setup
    _record(ledger, "r1")
    states = {"r1": FleetTimeout("status")}
    kwargs = dict(
        runner=Runner(states),
        ledger=ledger,
        wake_targets=targets,
        log_path=tmp_path / "w.jsonl",
        now=lambda: NOW,
        post=Post([]),
    )
    for _ in range(30):
        watch.run_watch_once(**kwargs)
    assert ledger.get("r1")["status_failures"] == 30
    assert ledger.open_count() == 1
    states["r1"] = "succeeded"
    for tick in range(15):
        ledger.close()
        ledger = Ledger(tmp_path / "ledger.sqlite3")
        kwargs["ledger"] = ledger
        summary = watch.run_watch_once(**kwargs)
        assert summary["checked"] == (1 if tick == 14 else 0)
    assert ledger.open_count() == 0
    assert ledger.get("r1")["status_failures"] == 0
    assert ledger.get("r1")["notified_state"] == "succeeded"


def test_delivered_targets_store_only_url_hashes(setup):
    import hashlib

    tmp_path, targets, ledger = setup
    _record(ledger, "r1", state="succeeded")
    watch.run_watch_once(
        runner=Runner({}),
        ledger=ledger,
        wake_targets=targets,
        log_path=tmp_path / "w.jsonl",
        now=lambda: NOW,
        post=Post([]),
    )
    stored = ledger.get("r1")["delivered_targets"]
    assert json.loads(stored) == [hashlib.sha256(targets["Builder"][0]["url"].encode()).hexdigest()]
    assert targets["Builder"][0]["key_file"] not in stored


def test_unexpected_wake_failure_preserves_delivered_targets_and_retry_cap(setup):
    tmp_path, targets, ledger = setup
    first_url = targets["Builder"][0]["url"]
    targets["Builder"].append({**targets["Builder"][0], "url": "https://example.com/second"})
    _record(ledger, "r1", state="succeeded")
    calls = []

    def post(url, key, body):
        calls.append(url)
        if url != first_url:
            raise RuntimeError("unexpected transport failure")
        return 200

    for _ in range(31):
        watch.run_watch_once(
            runner=Runner({}),
            ledger=ledger,
            wake_targets=targets,
            log_path=tmp_path / "w.jsonl",
            now=lambda: NOW,
            post=post,
        )
    assert calls.count(first_url) == 1
    assert len(calls) == 31
    assert ledger.get("r1")["wake_attempts"] == 30
    assert ledger.get("r1")["notified_state"] == "succeeded"


def test_row_log_failure_cannot_bypass_wake_retry_cap(setup):
    tmp_path, targets, ledger = setup
    _record(ledger, "r1", state="succeeded")
    post = Post([500] * 31)
    for _ in range(31):
        summary = watch.run_watch_once(
            runner=Runner({}),
            ledger=ledger,
            wake_targets=targets,
            log_path=tmp_path,
            now=lambda: NOW,
            post=post,
        )
    assert len(post.calls) == 30
    assert ledger.get("r1")["notified_state"] == "succeeded"
    assert summary == {"checked": 0, "woken": 0, "failed": 0}


def test_pass_budget_defers_remaining_rows(setup):
    tmp_path, targets, ledger = setup
    for rid in ("r1", "r2", "r3"):
        _record(ledger, rid)
    ticks = iter([0.0, 0.0, 0.0, 10.0, 10.0, 10.0])
    summary = watch.run_watch_once(
        runner=Runner({"r1": "running", "r2": "running", "r3": "running"}),
        ledger=ledger,
        wake_targets=targets,
        log_path=tmp_path / "w.jsonl",
        now=lambda: NOW,
        post=Post([]),
        budget_seconds=5.0,
        monotonic=lambda: next(ticks),
    )
    assert summary["checked"] == 2
    assert summary["deferred"] == 1


def test_polling_rotates_so_later_rows_are_reached(setup):
    tmp_path, targets, ledger = setup
    for rid in ("r1", "r2", "r3"):
        _record(ledger, rid)
    seen = []

    class OneShot:
        def status(self, request_id):
            seen.append(request_id)
            return {"schema": "t3-fleet.result.v1", "request_id": request_id, "state": "running"}

    for _ in range(3):
        clock = iter([0.0, 0.0, 10.0, 10.0])
        watch.run_watch_once(
            runner=OneShot(),
            ledger=ledger,
            wake_targets=targets,
            log_path=tmp_path / "w.jsonl",
            now=lambda: NOW,
            post=Post([]),
            budget_seconds=5.0,
            monotonic=clock.__next__,
        )
    assert seen == ["r1", "r2", "r3"]


def test_watcher_does_not_overwrite_a_newer_state(setup):
    tmp_path, targets, ledger = setup
    _record(ledger, "r1", state="succeeded")
    ledger.update_state("r1", "running", NOW)

    class Racing:
        def status(self, request_id):
            # The listener reopens the request while this status call is in flight.
            ledger.update_state(request_id, "needs_attention", NOW)
            return {"schema": "t3-fleet.result.v1", "request_id": request_id, "state": "succeeded"}

    post = Post([])
    watch.run_watch_once(
        runner=Racing(), ledger=ledger, wake_targets=targets, log_path=tmp_path / "w.jsonl", now=lambda: NOW, post=post
    )
    assert ledger.get("r1")["last_state"] == "needs_attention"
    assert post.calls == []


@pytest.mark.parametrize("listener_states", [("running",), ("succeeded", "running")], ids=["same-state", "aba"])
def test_stale_terminal_poll_cannot_close_a_newer_running_request(setup, listener_states):
    tmp_path, targets, ledger = setup
    _record(ledger, "r1")
    listener = Ledger(tmp_path / "ledger.sqlite3")

    class Racing:
        def status(self, request_id):
            for state in listener_states:
                listener.update_state(request_id, state, NOW)
            return {"schema": "t3-fleet.result.v1", "request_id": request_id, "state": "succeeded"}

    post = Post([])
    try:
        summary = watch.run_watch_once(
            runner=Racing(),
            ledger=ledger,
            wake_targets=targets,
            log_path=tmp_path / "w.jsonl",
            now=lambda: NOW,
            post=post,
        )
    finally:
        listener.close()
    assert ledger.get("r1")["last_state"] == "running"
    assert ledger.open_count(NOW) == 1
    assert post.calls == []
    assert summary == {"checked": 1, "woken": 0, "failed": 0}


def test_reopened_terminal_row_gets_no_stale_wake(setup, monkeypatch):
    tmp_path, targets, ledger = setup
    _record(ledger, "r1", state="succeeded")
    snapshot = ledger.pending_notifications

    def snapshot_then_reopen():
        # A follow-up sets its reopening marker after the watcher reads the terminal row.
        rows = snapshot()
        ledger.update_state("r1", c.REOPENING_STATE, NOW, owner_write=True)
        return rows

    monkeypatch.setattr(ledger, "pending_notifications", snapshot_then_reopen)
    post = Post([])
    watch.run_watch_once(
        runner=Runner({}),
        ledger=ledger,
        wake_targets=targets,
        log_path=tmp_path / "w.jsonl",
        now=lambda: NOW,
        post=post,
    )
    assert post.calls == []
    row = ledger.get("r1")
    assert row["last_state"] == c.REOPENING_STATE
    assert row["wake_attempts"] == 0


def test_reopen_between_targets_stops_the_remaining_wakes(setup):
    tmp_path, targets, ledger = setup
    targets["Builder"].append({**targets["Builder"][0], "url": "https://example.com/second"})
    _record(ledger, "r1", state="succeeded")
    post = Post([])

    def reopening_post(url, key, body):
        ledger.update_state("r1", c.REOPENING_STATE, NOW, owner_write=True)
        return post(url, key, body)

    watch.run_watch_once(
        runner=Runner({}),
        ledger=ledger,
        wake_targets=targets,
        log_path=tmp_path / "w.jsonl",
        now=lambda: NOW,
        post=reopening_post,
    )
    assert len(post.calls) == 1
    assert ledger.get("r1")["last_state"] == c.REOPENING_STATE


def test_pass_budget_leaves_room_for_the_slowest_row():
    from brigade.grokbot_t3.fleet import CALL_TIMEOUT

    # A row that starts just inside the budget may still make a status call and post to every target.
    slowest_row = CALL_TIMEOUT + watch.MAX_TARGETS * watch.POST_TIMEOUT
    assert watch.PASS_BUDGET_SECONDS + slowest_row < watch.UNIT_TIMEOUT_SECONDS


@pytest.mark.parametrize(
    "elapsed,enabled,expected_timeout,failed",
    [(0, True, 1, 0), (257.6, True, 0.4, 0), (258, True, None, 1), (301, True, None, 1), (0, False, None, 0)],
)
def test_flush_lock_wait_respects_unit_budget_after_request_wakes(
    setup, monkeypatch, elapsed, enabled, expected_timeout, failed
):
    from types import SimpleNamespace

    tmp_path, targets, ledger = setup
    _record(ledger, "r1", state="succeeded")
    runtime = SimpleNamespace(enabled=enabled, hub_token_file=tmp_path / "principal.token")
    monkeypatch.setattr(watch, "_wake_hub_inbox", lambda **kwargs: None)
    post = Post([])
    flushed = []

    def flush(action, **kwargs):
        assert action == "flush_credential"
        assert len(post.calls) == 1
        flushed.append(kwargs)

    monkeypatch.setattr(watch, "call_message", flush)
    ticks = iter([0, 0, elapsed])
    summary = watch.run_watch_once(
        runner=Runner({}),
        runtime=runtime,
        ledger=ledger,
        wake_targets=targets,
        log_path=tmp_path / "w.jsonl",
        post=post,
        monotonic=lambda: next(ticks),
    )
    assert summary["woken"] == 1 and summary["failed"] == failed
    if expected_timeout is None:
        assert flushed == []
    else:
        assert len(flushed) == 1
        assert flushed[0]["token_file"] == str(runtime.hub_token_file)
        assert flushed[0]["lock_timeout"] == pytest.approx(expected_timeout)
