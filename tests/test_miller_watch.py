"""Watch deduplication, read-only state and operator entry point regressions."""

import json
from datetime import timedelta
from unittest.mock import patch

import pytest

from brigade.miller import watch as w
from brigade.miller import __main__ as cli
from brigade.miller.contracts import MillerError
from brigade.miller.state import BridgeState
from tests.test_miller_state import NOW
from tests.test_miller_lifecycle import configured as configured


def insert(state, age=0, ident=1):
    with state.transaction() as c:
        c.execute(
            "INSERT INTO hub_inbox(id,sender,body,created_at,synced_at,subject) VALUES (?,?,?,?,?,?)",
            (
                ident,
                "agent:worker",
                "SECRET-BODY",
                (NOW + timedelta(minutes=age)).isoformat(),
                NOW.isoformat(),
                "question",
            ),
        )


def run(configured, now, **kw):
    target, _, _ = configured
    with patch.object(w, "probe_route", return_value=[]):
        return w.run_watch_once(target, now=now, **kw)


def test_watch_fresh_stale_dedupe_and_realert(configured):
    _, runtime, _ = configured
    with BridgeState(runtime.paths.state_dir) as state:
        insert(state)
        state.hub_set("last_sync_ok_at", NOW.isoformat())
    with patch.object(w, "send", return_value=0) as send:
        assert run(configured, NOW + timedelta(minutes=29))["stale"] == 0
        first = run(configured, NOW + timedelta(minutes=45))
        assert "stale_inbox" in [a["kind"] for a in first["alerts"]]
        assert "SECRET" not in json.dumps(first)
        assert run(configured, NOW + timedelta(minutes=55))["alerts"] == []
        assert "stale_inbox" in [a["kind"] for a in run(configured, NOW + timedelta(hours=3))["alerts"]]
        assert send.call_count >= 2
    path = runtime.paths.state_dir / "inbox-watch.json"
    assert path.stat().st_mode & 0o777 == 0o600


def test_watch_failed_send_retries_and_dry_run(configured):
    _, runtime, _ = configured
    with BridgeState(runtime.paths.state_dir) as state:
        insert(state)
    path = runtime.paths.state_dir / "inbox-watch.json"
    with patch.object(w, "send", return_value=4):
        assert run(configured, NOW + timedelta(hours=1))["ok"] is False
        assert not path.exists()
    with patch.object(w, "send", side_effect=AssertionError("dry run sent")):
        assert all(
            a["result"] == "would_send" for a in run(configured, NOW + timedelta(hours=1), dry_run=True)["alerts"]
        )
        assert not path.exists()
    with patch.object(w, "send", return_value=0):
        assert run(configured, NOW + timedelta(hours=1))["ok"]
        assert path.exists()


def test_watch_idle_credential_and_unanswered_wake(configured):
    target, runtime, _ = configured
    with BridgeState(runtime.paths.state_dir) as state:
        state.log_call("fleet_digest", True, 5, clock=lambda: NOW)
        state.hub_set("last_sync_error", "hub_credential_invalid")
        with state.transaction() as c:
            c.execute("INSERT INTO tracked_requests(request_id) VALUES ('request-example')")
            c.execute("INSERT INTO wake_attempts VALUES (?, 'normal')", ((NOW + timedelta(minutes=1)).timestamp(),))
    with patch.object(w, "send", return_value=0), patch.object(w, "probe_route", return_value=["edge"]):
        first = w.run_watch_once(target, now=NOW + timedelta(hours=4))
        kinds = {a["kind"] for a in first["alerts"]}
        assert {"miller_idle", "hub_credential_invalid", "wake_unanswered", "route_down"} <= kinds
        assert "hub_sync_stale" not in kinds
        assert w.run_watch_once(target, now=NOW + timedelta(hours=4, minutes=1))["alerts"] == []


def test_watch_never_writes_database_and_skips_missing_notifier(configured):
    target, runtime, path = configured
    runtime_path = path.parent.parent.parent.parent / "unused"
    payload = json.loads(path.read_text())
    from pathlib import Path

    runtime_path = Path(payload["runtime_path"])
    document = json.loads(runtime_path.read_text())
    document.pop("notify_command")
    runtime_path.write_text(json.dumps(document))
    with BridgeState(runtime.paths.state_dir) as state:
        insert(state)
    database = runtime.paths.state_dir / "bridge.sqlite3"
    before = database.read_bytes()
    with patch.object(w, "send", side_effect=AssertionError("no notifier")):
        report = run((target, runtime, path), NOW + timedelta(hours=1))
    assert report["ok"]
    assert all(a["result"] == "skipped" for a in report["alerts"])
    assert database.read_bytes() == before
    assert not (runtime.paths.state_dir / "inbox-watch.json").exists()


def test_calls_filters_gaps_and_cli(configured, capsys):
    target, runtime, _ = configured
    with BridgeState(runtime.paths.state_dir) as state:
        for minutes, tool in [
            (0, "initialize"),
            (1, "tools/list"),
            (2, "fleet_digest"),
            (40, "inbox_list"),
            (41, "fleet_digest"),
            (200, "fleet_status"),
        ]:
            state.log_call(tool, True, 5, clock=lambda minutes=minutes: NOW + timedelta(minutes=minutes))
    assert cli.main(["calls", "--target", str(target), "--json", "--tool", "fleet_digest"]) == 0
    assert len(json.loads(capsys.readouterr().out)) == 2
    assert cli.main(["calls", "--target", str(target), "--gaps", "--json", "--gap-minutes", "30"]) == 0
    report = json.loads(capsys.readouterr().out)
    assert (report["calls"], report["idle_minutes"]) == (6, 197.0)
    assert [g["minutes"] for g in report["gaps"]] == [38.0, 159.0]


@pytest.mark.parametrize("queued", [False, True])
def test_notify_hub_mapping_and_queued(configured, capsys, queued):
    target, _, _ = configured
    with patch.object(cli.HubInbox, "notify", return_value=None if queued else {"id": 7}) as notify:
        assert cli.main(["notify", "--target", str(target), "--from", "worker", "--thread", "topic", "body"]) == 0
    value = json.loads(capsys.readouterr().out)
    assert value["ok"] and value["id"] == (None if queued else 7)
    assert notify.call_args.args[:3] == ("worker", "body", "topic")
    if queued:
        assert value["queued"] and len(value["client_id"]) == 32


@pytest.mark.parametrize(
    "responses,code,error",
    [
        ([([], 1)], 3, "reply_timeout"),
        ([MillerError("hub_unavailable")], 2, "inbox_unavailable"),
        ([([{"id": 2, "body": "answer"}], 2)], 0, None),
    ],
)
def test_notify_wait_and_timeout(configured, capsys, responses, code, error):
    target, _, _ = configured
    with patch.object(cli.HubInbox, "wait_replies", side_effect=responses):
        assert cli.main(["notify", "--target", str(target), "--wait", "1", "--timeout", "0"]) == code
    value = json.loads(capsys.readouterr().out)
    assert value.get("error") == error


def test_notify_wait_passes_its_deadline(configured, capsys):
    import time

    target, _, _ = configured
    seen = []

    def wait(msg_id, cursor, *, deadline=None):
        seen.append(deadline)
        return [{"id": 2, "body": "answer"}], 2

    started = time.monotonic()
    with patch.object(cli.HubInbox, "wait_replies", side_effect=wait):
        assert cli.main(["notify", "--target", str(target), "--wait", "1", "--timeout", "5"]) == 0
    # Each poll is bounded by the CLI deadline (at least 2 s so --timeout 0 still checks once).
    assert seen and started + 2 <= seen[0] <= time.monotonic() + 5
    capsys.readouterr()


def test_probe_route_failures_are_bounded(configured):
    target, runtime, _ = configured
    from brigade.miller.lifecycle import _load_config

    cfg, _ = _load_config(target)
    with patch.object(w, "_probe", return_value=(0, {})) as probe:
        assert w.probe_route(cfg, runtime) == ["origin", "edge"]
        assert probe.call_count == 2


def test_send_stdin_and_no_output(configured):
    _, runtime, _ = configured
    with patch.object(w.subprocess, "run") as execute:
        execute.return_value.returncode = 0
        assert w.send(runtime.notify_command, {"title": "title", "text": "message"}) == 0
    assert execute.call_args.args[0] == list(runtime.notify_command)
    assert json.loads(execute.call_args.kwargs["input"])["body"] == "message"


def test_notify_requires_message_without_reading_tty(configured, capsys):
    target, _, _ = configured
    with patch.object(cli.sys, "stdin") as stdin:
        stdin.isatty.return_value = True
        assert cli.main(["notify", "--target", str(target), "--from", "worker"]) == 2
        stdin.buffer.read.assert_not_called()
    assert json.loads(capsys.readouterr().out)["error"] == "message_required"


def test_notify_transient_wait_recovers(configured, capsys):
    target, _, _ = configured
    elapsed = [0]

    def sleep(seconds):
        elapsed[0] += seconds

    responses = [MillerError("hub_unavailable"), ([], 1), ([{"id": 2, "body": "answer"}], 2)]
    with (
        patch.object(cli.HubInbox, "wait_replies", side_effect=responses),
        patch.object(cli.time, "monotonic", side_effect=lambda: elapsed[0]),
        patch.object(cli.time, "sleep", side_effect=sleep),
    ):
        assert cli.main(["notify", "--target", str(target), "--wait", "1", "--timeout", "9"]) == 0
    assert json.loads(capsys.readouterr().out)["replies"][0]["body"] == "answer"
