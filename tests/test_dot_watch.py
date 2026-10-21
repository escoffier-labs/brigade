"""Watch deduplication, read-only state and operator entry point regressions."""

from tests.dot_support import requires_local_state

import asyncio
import base64
import json
from datetime import timedelta
from unittest.mock import patch

import pytest

from brigade.dot import watch as w
from brigade.dot import __main__ as cli
from brigade.dot.contracts import DotError
from brigade.dot.state import BridgeState
from brigade.dot.events import EventService, enqueue
from brigade.dot.hub_inbox import HubInbox
from brigade import fleet_client
from brigade.fleet_client_message_credentials import message_scope, message_settings
from tests.test_dot_state import NOW
from tests.test_dot_lifecycle import configured as configured


@pytest.fixture
def watch_settings():
    return dict(hub_url="https://hub.invalid", node_token="fixture-node", admin_token="")


@pytest.fixture(autouse=True)
def scoped_hub(configured, monkeypatch, watch_settings):
    _, runtime, _ = configured
    # Redirect the fixture token out of the runtime document's placeholder path.
    path = runtime.paths.state_dir.parent / "watch.token"
    path.write_text("fixture-watch-credential-" * 2)
    path.chmod(0o600)
    _, _, instance = configured
    from pathlib import Path

    document_path = Path(json.loads(instance.read_text())["runtime_path"])
    document = json.loads(document_path.read_text())
    document["hub"]["message_token_file"] = str(path)
    document_path.write_text(json.dumps(document))
    monkeypatch.setattr(fleet_client, "load_fleet_settings", lambda: dict(watch_settings))
    monkeypatch.setattr(fleet_client, "resolve_node_id", lambda: "node-a")
    return message_scope(*message_settings(str(path))[:2])


def insert(state, scope, age=0, ident=1):
    with state.transaction() as c:
        c.execute(
            "INSERT INTO hub_inbox(scope,id,sender,body,created_at,synced_at,subject) VALUES (?,?,?,?,?,?,?)",
            (
                scope,
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


def wake_service(state, runtime, posted):
    async def post(url, body, headers):
        posted.append(json.loads(body)["data"]["msg_id"])
        return 200, b"{}"

    with state.transaction() as c:
        c.execute(
            "INSERT INTO subscriptions VALUES (?,?,?,?,?,?,?,?,?,?)",
            (
                "sub-example",
                "owner-example",
                "fleet.inbox.message",
                "{}",
                "https://callback.invalid",
                "whsec_" + base64.b64encode(b"k" * 32).decode(),
                None,
                None,
                None,
                0,
            ),
        )
    return EventService(
        state,
        fleet_status=lambda _: {},
        hub=HubInbox(state, runtime.hub),
        hub_poll_seconds=30,
        callback_hosts=frozenset(),
        post=post,
    )


@requires_local_state
def test_watch_wake_status_is_bound_to_delivered_hub_scope(configured, scoped_hub, watch_settings):
    target, _, _ = configured
    runtime = w._load_config(target)[1]
    posted = []
    with BridgeState(runtime.paths.state_dir) as state:
        service = wake_service(state, runtime, posted)
        with state.transaction() as c:
            enqueue(
                c, "fleet.inbox.message", {"msg_id": 50}, "normal", "hub-inbox:" + scoped_hub + ":50", NOW.timestamp()
            )
        asyncio.run(service.deliver(NOW.timestamp()))
        assert posted == [50]
        assert w.read_bridge(runtime)[-1] == NOW
        watch_settings["hub_url"] = "https://b.invalid"
        other = service.hub.scope()
        assert w.read_bridge(runtime)[-1] is None
        with patch.object(w, "send", return_value=0):
            report = run(configured, NOW + timedelta(minutes=31))
        assert "wake_unanswered" not in {a["kind"] for a in report["alerts"]}
        with state.transaction() as c:
            enqueue(c, "fleet.inbox.message", {"msg_id": 1}, "normal", "hub-inbox:" + other + ":1", NOW.timestamp() + 1)
        asyncio.run(service.deliver(NOW.timestamp() + 1))
        # A's normal wake still debounces B globally, without becoming B's diagnostic wake.
        assert posted == [50]
        assert w.read_bridge(runtime)[-1] is None
        asyncio.run(service.deliver(NOW.timestamp() + 300))
        assert posted == [50, 1]
        assert w.read_bridge(runtime)[-1] == NOW + timedelta(minutes=5)
        watch_settings["hub_url"] = "https://hub.invalid/"
        assert w.read_bridge(runtime)[-1] == NOW


@requires_local_state
def test_legacy_wake_rows_keep_global_budget_without_diagnostic_scope(configured, scoped_hub):
    target, _, _ = configured
    runtime = w._load_config(target)[1]
    legacy = [(NOW.timestamp(), "urgent")] * 30
    with BridgeState(runtime.paths.state_dir) as state:
        with state.transaction() as c:
            c.execute("DROP TABLE wake_attempts")
            c.execute("CREATE TABLE wake_attempts (timestamp REAL NOT NULL, priority TEXT NOT NULL)")
            c.executemany("INSERT INTO wake_attempts VALUES (?,?)", legacy)
    # The read-only watcher must also ignore unknown origins before migration.
    assert w.read_bridge(runtime)[-1] is None
    # Reopen through the production initializer, as on upgrade.
    with BridgeState(runtime.paths.state_dir) as state:
        assert w.read_bridge(runtime)[-1] is None
        with state.transaction() as c:
            assert [tuple(r) for r in c.execute("SELECT timestamp,priority FROM wake_attempts")] == legacy
        posted = []
        service = wake_service(state, runtime, posted)
        with state.transaction() as c:
            enqueue(
                c, "fleet.inbox.message", {"msg_id": 1}, "urgent", "hub-inbox:" + scoped_hub + ":1", NOW.timestamp()
            )
        asyncio.run(service.deliver(NOW.timestamp() + 1))
        assert posted == []
        assert w.read_bridge(runtime)[-1] is None
        with state.transaction() as c:
            assert [tuple(r) for r in c.execute("SELECT timestamp,priority FROM wake_attempts")] == legacy
            assert c.execute("SELECT next_attempt FROM event_deliveries").fetchone()[0] == NOW.timestamp() + 3600
        asyncio.run(service.deliver(NOW.timestamp() + 3601))
        assert posted == [1]
        assert w.read_bridge(runtime)[-1] == NOW + timedelta(seconds=3601)


@requires_local_state
def test_watch_fresh_stale_dedupe_and_realert(configured, scoped_hub):
    _, runtime, _ = configured
    with BridgeState(runtime.paths.state_dir) as state:
        insert(state, scoped_hub)
        state.hub_set("last_sync_ok_at", NOW.isoformat(), scope=scoped_hub)
    with patch.object(w, "send", return_value=0) as send:
        assert run(configured, NOW + timedelta(minutes=29))["stale"] == 0
        first = run(configured, NOW + timedelta(minutes=45))
        assert "stale_inbox" in [a["kind"] for a in first["alerts"]]
        assert "SECRET" not in json.dumps(first)
        assert run(configured, NOW + timedelta(minutes=55))["alerts"] == []
        assert "stale_inbox" in [a["kind"] for a in run(configured, NOW + timedelta(hours=3))["alerts"]]
        assert send.call_count >= 2
    path = runtime.paths.state_dir / ("inbox-watch-" + scoped_hub + ".json")
    assert path.stat().st_mode & 0o777 == 0o600


@requires_local_state
def test_watch_failed_send_retries_and_dry_run(configured, scoped_hub):
    _, runtime, _ = configured
    with BridgeState(runtime.paths.state_dir) as state:
        insert(state, scoped_hub)
    path = runtime.paths.state_dir / ("inbox-watch-" + scoped_hub + ".json")
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


@requires_local_state
def test_watch_idle_credential_and_unanswered_wake(configured, scoped_hub):
    target, runtime, _ = configured
    with BridgeState(runtime.paths.state_dir) as state:
        state.log_call("fleet_digest", True, 5, clock=lambda: NOW)
        state.hub_set("last_sync_error", "hub_credential_invalid", scope=scoped_hub)
        with state.transaction() as c:
            c.execute("INSERT INTO tracked_requests(request_id) VALUES ('request-example')")
            c.execute(
                "INSERT INTO wake_attempts(timestamp,priority,source) VALUES (?, 'normal', ?)",
                ((NOW + timedelta(minutes=1)).timestamp(), "hub-inbox:" + scoped_hub + ":1"),
            )
    with patch.object(w, "send", return_value=0), patch.object(w, "probe_route", return_value=["edge"]):
        first = w.run_watch_once(target, now=NOW + timedelta(hours=4))
        kinds = {a["kind"] for a in first["alerts"]}
        assert {"dot_idle", "hub_credential_invalid", "wake_unanswered", "route_down"} <= kinds
        assert "hub_sync_stale" not in kinds
        assert w.run_watch_once(target, now=NOW + timedelta(hours=4, minutes=1))["alerts"] == []


@requires_local_state
@pytest.mark.parametrize("missing", [False, True])
def test_watch_invalid_agent_file_alerts_without_message_authority(configured, scoped_hub, missing):
    target, runtime, _ = configured
    active_runtime = w._load_config(target)[1]
    token_file = active_runtime.hub.message_token_file
    with BridgeState(runtime.paths.state_dir) as state:
        insert(state, scoped_hub, age=-60, ident=50)
        state.hub_set("last_sync_ok_at", NOW.isoformat(), scope=scoped_hub)
        state.hub_set("last_sync_error", "hub_credential_invalid", scope=scoped_hub)
    database = runtime.paths.state_dir / "bridge.sqlite3"
    before = database.read_bytes()
    old_state = runtime.paths.state_dir / ("inbox-watch-" + scoped_hub + ".json")
    old_state.write_text(json.dumps({"sync": {"last_credential_alert": NOW.isoformat()}}))
    legacy = runtime.paths.state_dir / "inbox-watch.json"
    legacy.write_text(old_state.read_text())
    if missing:
        token_file.unlink()
    else:
        token_file.write_text("malformed credential material")
    # Actual credential loading must fail before any message or route state is read.
    with (
        patch.object(w, "read_bridge", side_effect=AssertionError("unproven inbox read")),
        patch.object(w, "probe_route", side_effect=AssertionError("unproven route state")),
        patch.object(w, "send", return_value=0) as send,
    ):
        first = w.run_watch_once(target, now=NOW)
        assert [a["kind"] for a in first.get("alerts", [])] == ["hub_credential_invalid"]
        assert first["ok"] and first["alerts"][0]["result"] == "sent"
        text = json.dumps(first)
        for private in (str(token_file), "malformed credential material", "SECRET", "question", "#50"):
            assert private not in text
        assert "Last successful sync" not in text
        assert w.run_watch_once(target, now=NOW + timedelta(minutes=1))["alerts"] == []
        assert len(w.run_watch_once(target, now=NOW + timedelta(hours=2))["alerts"]) == 1
        assert send.call_count == 2
    assert database.read_bytes() == before
    assert old_state.read_bytes() == legacy.read_bytes()
    health = runtime.paths.state_dir / "inbox-watch-health.json"
    assert json.loads(health.read_text()) == {"last_credential_alert": (NOW + timedelta(hours=2)).isoformat()}
    assert health.stat().st_mode & 0o777 == 0o600


@requires_local_state
@pytest.mark.parametrize("mode", ["dry_run", "send_failed", "no_notifier"])
def test_watch_invalid_agent_file_does_not_dedupe_unsent_health_alert(configured, mode):
    target, runtime, instance = configured
    active_runtime = w._load_config(target)[1]
    active_runtime.hub.message_token_file.unlink()
    if mode == "no_notifier":
        from pathlib import Path

        runtime_path = Path(json.loads(instance.read_text())["runtime_path"])
        document = json.loads(runtime_path.read_text())
        document.pop("notify_command")
        runtime_path.write_text(json.dumps(document))
    with patch.object(w, "send", return_value=4) as send:
        report = w.run_watch_once(target, now=NOW, dry_run=mode == "dry_run")
        assert report.get("alerts"), report
        assert (
            report["alerts"][0]["result"]
            == {"dry_run": "would_send", "send_failed": "send_failed:4", "no_notifier": "skipped"}[mode]
        )
        assert report["ok"] is (mode != "send_failed")
        assert send.call_count == (1 if mode == "send_failed" else 0)
    assert not (runtime.paths.state_dir / "inbox-watch-health.json").exists()


@requires_local_state
def test_watch_never_writes_database_and_skips_missing_notifier(configured, scoped_hub):
    target, runtime, path = configured
    runtime_path = path.parent.parent.parent.parent / "unused"
    payload = json.loads(path.read_text())
    from pathlib import Path

    runtime_path = Path(payload["runtime_path"])
    document = json.loads(runtime_path.read_text())
    document.pop("notify_command")
    runtime_path.write_text(json.dumps(document))
    with BridgeState(runtime.paths.state_dir) as state:
        insert(state, scoped_hub)
    database = runtime.paths.state_dir / "bridge.sqlite3"
    before = database.read_bytes()
    with patch.object(w, "send", side_effect=AssertionError("no notifier")):
        report = run((target, runtime, path), NOW + timedelta(hours=1))
    assert report["ok"]
    assert all(a["result"] == "skipped" for a in report["alerts"])
    assert database.read_bytes() == before
    assert not (runtime.paths.state_dir / ("inbox-watch-" + scoped_hub + ".json")).exists()


@requires_local_state
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


@requires_local_state
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


@requires_local_state
@pytest.mark.parametrize(
    "responses,code,error",
    [
        ([([], 1)], 3, "reply_timeout"),
        ([DotError("hub_unavailable")], 2, "inbox_unavailable"),
        ([([{"id": 2, "body": "answer"}], 2)], 0, None),
    ],
)
def test_notify_wait_and_timeout(configured, capsys, responses, code, error):
    target, _, _ = configured
    with patch.object(cli.HubInbox, "wait_replies", side_effect=responses):
        assert cli.main(["notify", "--target", str(target), "--wait", "1", "--timeout", "0"]) == code
    value = json.loads(capsys.readouterr().out)
    assert value.get("error") == error


@requires_local_state
def test_notify_wait_passes_its_deadline(configured, capsys):
    import time

    target, _, _ = configured
    seen = []

    def wait(msg_id, cursor, *, deadline=None, authority=None):
        seen.append(deadline)
        return [{"id": 2, "body": "answer"}], 2

    started = time.monotonic()
    with patch.object(cli.HubInbox, "wait_replies", side_effect=wait):
        assert cli.main(["notify", "--target", str(target), "--wait", "1", "--timeout", "5"]) == 0
    # Each poll is bounded by the CLI deadline (at least 2 s so --timeout 0 still checks once).
    assert seen and started + 2 <= seen[0] <= time.monotonic() + 5
    capsys.readouterr()


@requires_local_state
def test_probe_route_failures_are_bounded(configured):
    target, runtime, _ = configured
    from brigade.dot.lifecycle import _load_config

    cfg, _ = _load_config(target)
    with patch.object(w, "_probe", return_value=(0, {})) as probe:
        assert w.probe_route(cfg, runtime) == ["origin", "edge"]
        assert probe.call_count == 2


@requires_local_state
def test_send_stdin_and_no_output(configured):
    _, runtime, _ = configured
    with patch.object(w.subprocess, "run") as execute:
        execute.return_value.returncode = 0
        assert w.send(runtime.notify_command, {"title": "title", "text": "message"}) == 0
    assert execute.call_args.args[0] == list(runtime.notify_command)
    assert json.loads(execute.call_args.kwargs["input"])["body"] == "message"


@requires_local_state
def test_notify_requires_message_without_reading_tty(configured, capsys):
    target, _, _ = configured
    with patch.object(cli.sys, "stdin") as stdin:
        stdin.isatty.return_value = True
        assert cli.main(["notify", "--target", str(target), "--from", "worker"]) == 2
        stdin.buffer.read.assert_not_called()
    assert json.loads(capsys.readouterr().out)["error"] == "message_required"


@requires_local_state
def test_notify_transient_wait_recovers(configured, capsys):
    target, _, _ = configured
    elapsed = [0]

    def sleep(seconds):
        elapsed[0] += seconds

    responses = [DotError("hub_unavailable"), ([], 1), ([{"id": 2, "body": "answer"}], 2)]
    with (
        patch.object(cli.HubInbox, "wait_replies", side_effect=responses),
        patch.object(cli.time, "monotonic", side_effect=lambda: elapsed[0]),
        patch.object(cli.time, "sleep", side_effect=sleep),
    ):
        assert cli.main(["notify", "--target", str(target), "--wait", "1", "--timeout", "9"]) == 0
    assert json.loads(capsys.readouterr().out)["replies"][0]["body"] == "answer"


@requires_local_state
def test_watch_cache_and_dedupe_endpoint_walk(configured, scoped_hub, watch_settings):
    target, runtime, _ = configured
    with BridgeState(runtime.paths.state_dir):
        pass
    legacy = runtime.paths.state_dir / "inbox-watch.json"
    legacy.write_text('{"inbox":{"50":"2026-01-01T01:00:00+00:00"}}')
    original = legacy.read_bytes()
    with BridgeState(runtime.paths.state_dir) as state:
        insert(state, scoped_hub, ident=50)
    with patch.object(w, "send", return_value=0):
        assert any(a["kind"] == "stale_inbox" for a in run(configured, NOW + timedelta(hours=1))["alerts"])
        watch_settings["hub_url"] = "https://b.invalid"
        active_runtime = w._load_config(target)[1]
        other = message_scope(*message_settings(str(active_runtime.hub.message_token_file))[:2])
        with BridgeState(runtime.paths.state_dir) as state:
            insert(state, other, ident=1)
            insert(state, other, ident=50)
        assert [row[0] for row in w.read_bridge(active_runtime)[0]] == [1, 50]
        assert any(a["kind"] == "stale_inbox" for a in run(configured, NOW + timedelta(hours=1))["alerts"])
        watch_settings["hub_url"] = "https://hub.invalid/"
        assert not any(a["kind"] == "stale_inbox" for a in run(configured, NOW + timedelta(hours=1))["alerts"])
    assert legacy.read_bytes() == original


@requires_local_state
def test_watch_does_not_send_cached_ids_after_scope_changes_in_probe(configured, scoped_hub, watch_settings):
    target, runtime, _ = configured
    with BridgeState(runtime.paths.state_dir) as state:
        insert(state, scoped_hub)

    def switch(*args):
        watch_settings["hub_url"] = "https://b.invalid"
        return []

    with patch.object(w, "send") as send, patch.object(w, "probe_route", side_effect=switch):
        assert w.run_watch_once(target, now=NOW + timedelta(hours=1)) == {
            "ok": False,
            "error": "bridge_state_unavailable",
        }
        send.assert_not_called()


@requires_local_state
def test_watch_rejects_partial_result_if_endpoint_changes_in_send(configured, scoped_hub, watch_settings):
    target, runtime, _ = configured
    with BridgeState(runtime.paths.state_dir) as state:
        insert(state, scoped_hub)
        state.hub_set("last_sync_ok_at", (NOW + timedelta(hours=1)).isoformat(), scope=scoped_hub)

    def switch(*args, **kwargs):
        watch_settings["hub_url"] = "https://b.invalid"
        return 0

    with patch.object(w, "send", side_effect=switch), patch.object(w, "probe_route", return_value=[]):
        assert w.run_watch_once(target, now=NOW + timedelta(hours=1)) == {
            "ok": False,
            "error": "bridge_state_unavailable",
        }


@requires_local_state
def test_notify_cli_does_not_mix_confirmation_and_count_across_endpoints(configured, watch_settings, capsys):
    target, _, _ = configured

    def changed(*args, **kwargs):
        watch_settings["hub_url"] = "https://b.invalid"
        return {"id": 50}

    with patch.object(cli.HubInbox, "notify", side_effect=changed):
        assert cli.main(["notify", "--target", str(target), "--from", "worker", "body"]) == 2
    assert json.loads(capsys.readouterr().out) == {"ok": False, "error": "inbox_unavailable"}


@requires_local_state
def test_rotation_during_alert_preparation_stops_before_subprocess(configured, scoped_hub, watch_settings, monkeypatch):
    target, runtime, _ = configured
    with BridgeState(runtime.paths.state_dir) as state:
        insert(state, scoped_hub)
    original_dumps = json.dumps
    calls = []

    def dumps(value, *args, **kwargs):
        if isinstance(value, dict) and value.get("source") == "dot-inbox-watch":
            watch_settings["hub_url"] = "https://other.invalid"
        return original_dumps(value, *args, **kwargs)

    monkeypatch.setattr(w.json, "dumps", dumps)
    monkeypatch.setattr(w, "probe_route", lambda *a: [])
    monkeypatch.setattr(w.subprocess, "run", lambda *a, **k: calls.append(a) or type("Result", (), {"returncode": 0})())
    assert w.run_watch_once(target, now=NOW + timedelta(hours=1))["ok"] is False
    assert calls == []
