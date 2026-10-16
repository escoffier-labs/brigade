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
def setup(tmp_path):
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
def test_rejects_bad_wake_config(tmp_path, targets):
    path = _owner_file(tmp_path / "wake.json", json.dumps({"schema": watch.WAKE_SCHEMA, "targets": targets}))
    with pytest.raises(c.T3DispatchError):
        watch.load_wake_config(path)
