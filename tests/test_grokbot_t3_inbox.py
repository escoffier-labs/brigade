"""Fleet Hub message tools through the dispatch connector boundary."""

from dataclasses import replace
import json
import sys

import pytest

from brigade import fleet_client, fleet_client_messages as messages
from brigade.fleet_client_message_credentials import FleetMessageHubError
from brigade.grokbot_t3 import contracts as c
from tests import test_grokbot_t3_tools

dispatch_make = test_grokbot_t3_tools.make

HUB_NODE_ID = "00000000-0000-4000-8000-000000000001"

HUB_TOOLS = {
    "inbox_list": {},
    "inbox_ack": {"ids": [1]},
    "inbox_reply": {"msg_id": 1, "body": "response"},
    "message_send": {"to": "host-a", "subject": "subject", "body": "body"},
    "message_status": {"ids": [1]},
}


@pytest.fixture
def hub(dispatch_make, tmp_path, monkeypatch):
    token_file = tmp_path / "principal.token"
    token = "x" * 64
    token_file.write_text(token)
    token_file.chmod(0o600)
    tools, runner, ledger = dispatch_make(hub_token_file=str(token_file), hub_recipients={"host-a": HUB_NODE_ID})
    monkeypatch.setattr(fleet_client, "load_fleet_settings", lambda: {"hub_url": "https://hub.example.test"})
    monkeypatch.setattr(fleet_client, "resolve_node_id", lambda: "node-id")
    monkeypatch.setattr(messages, "_spool_path", lambda *args: tmp_path / "spool" / "messages.jsonl")
    monkeypatch.setattr(fleet_client, "spool_path", lambda *args: tmp_path / "spool" / "node.jsonl")
    calls = []
    row = dict(
        id=1,
        sender="host-a",
        recipient="agent:example",
        body="incoming body",
        subject="incoming subject",
        created_at="2026-10-20T12:00:00+00:00",
        acked_at=None,
    )

    def request(hub_url, credential, body, *, timeout=None):
        assert credential == token
        calls.append(dict(body, timeout=timeout))
        action = body["action"]
        if action == "list":
            rows = [row] if body["after"] < row["id"] and (not body["unacked"] or row["acked_at"] is None) else []
            return 200, {"messages": rows, "next_cursor": row["id"] if rows else body["after"]}
        if action == "ack":
            row["acked_at"] = "2026-10-20T12:01:00+00:00"
            return 200, {"message": row}
        if action == "sent":
            return 200, {"messages": [dict(row, id=2, recipient="host-a")], "next_cursor": 2}
        return 200, {
            "message": dict(
                row, id=2, client_id=body["client_id"], sender="agent:example", recipient=body.get("to", "host-a")
            )
        }

    monkeypatch.setattr(messages, "_request", request)
    return tools, calls, row, token, token_file, ledger


@pytest.mark.parametrize("name,args", HUB_TOOLS.items())
def test_missing_hub_config_is_closed_without_breaking_dispatch(dispatch_make, name, args):
    tools, _, _ = dispatch_make()
    assert tools.call_tool(name, args)["error"]["code"] == "hub-not-configured"
    assert "error" not in tools.call_tool("t3_capacity", {})


def test_hub_tools_list_ack_reply_and_status(hub):
    tools, calls, _, token, _, ledger = hub
    listing = tools.call_tool("inbox_list", {})
    assert listing["content_trust"] == "untrusted"
    assert listing["order"] == "oldest first"
    assert listing["messages"][0]["body"] == "incoming body"
    assert listing["unacked_total"] == 1
    reply = tools.call_tool("inbox_reply", {"msg_id": 1, "body": "response"})
    assert reply["id"] == 2 and reply["msg_id"] == 1
    assert not any(call["action"] == "ack" for call in calls)
    status = tools.call_tool("message_status", {"ids": [2]})
    assert status["messages"][0]["to"] == "host-a"
    assert "body" not in status["messages"][0]
    assert tools.call_tool("inbox_ack", {"ids": [1]})["acked"] == [1]
    assert token not in json.dumps([listing, reply, status, ledger.recent(None, 20)])


@pytest.mark.parametrize("recipient", ["host-a", "00000000-0000-4000-8000-000000000001", "agent:example"])
def test_send_accepts_hub_recipients_and_preserves_client_id(hub, recipient):
    tools, calls, _, _, _, _ = hub
    args = {"to": recipient, "subject": "subject", "body": "body", "client_id": "stable-id"}
    first = tools.call_tool("message_send", args)
    second = tools.call_tool("message_send", args)
    assert first["client_id"] == second["client_id"] == "stable-id"
    sends = [call for call in calls if call["action"] == "send"]
    assert len(sends) == 2
    expected = HUB_NODE_ID if recipient == "host-a" else recipient
    assert first["recipient"] == expected
    assert all(call["to"] == expected and call["client_id"] == "stable-id" for call in sends)


@pytest.mark.parametrize("name", ["inbox_ack", "inbox_reply", "message_send"])
def test_hub_mutations_honor_enabled(hub, name):
    tools, calls, _, _, _, _ = hub
    tools.runtime = replace(tools.runtime, enabled=False)
    assert tools.call_tool(name, HUB_TOOLS[name])["error"]["code"] == "dispatch-disabled"
    assert calls == []
    assert "error" not in tools.call_tool("inbox_list", {})


def test_grant_code_and_raw_secret_errors_are_safe(hub, monkeypatch, caplog):
    tools, _, _, token, _, _ = hub

    def refused(*args, **kwargs):
        raise FleetMessageHubError(token, status=403, code="message_recipient_not_permitted")

    monkeypatch.setattr(messages, "send", refused)
    result = tools.call_tool("message_send", HUB_TOOLS["message_send"])
    assert result["error"]["hub_code"] == "message_recipient_not_permitted"
    assert result["error"]["code"] == "hub-error"
    assert token not in json.dumps(result) + caplog.text
    assert token not in json.dumps(c.T3DispatchError("hub-error", hub_code=token).public_error())


def test_body_byte_cap_and_unknown_fields_rejected_before_transport(hub):
    tools, calls, _, _, _, _ = hub
    for args in ({"body": "é" * 4097}, {"body": "ok", "extra": True}):
        value = dict(HUB_TOOLS["message_send"], **args)
        assert tools.call_tool("message_send", value)["error"]["code"] == "invalid_request"
    assert calls == []


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX principal file permissions")
@pytest.mark.parametrize("mode", ["missing", "readable", "symlink"])
def test_principal_credential_is_owner_only_and_errors_are_closed(hub, mode):
    tools, _, _, token, path, _ = hub
    if mode == "missing":
        path.unlink()
    elif mode == "readable":
        path.chmod(0o644)
    else:
        real = path.with_suffix(".real")
        path.rename(real)
        path.symlink_to(real)
    result = tools.call_tool("inbox_list", {})
    assert result["error"]["code"] == "hub-error"
    assert result["error"]["hub_code"] == "token_file_invalid"
    assert token not in json.dumps(result)


@pytest.mark.usefixtures("t3_owner_reads", "t3_watch_log")
def test_hub_wake_retries_failed_target_and_persists_only_success_cursor(hub, tmp_path):
    from brigade.grokbot_t3 import watch
    from brigade.grokbot_t3.ledger import Ledger
    from tests.test_grokbot_t3_watch import Post

    tools, calls, row, token, _, ledger = hub
    key = tmp_path / "wake.key"
    key.write_text("synthetic-wake-key")
    key.chmod(0o600)
    targets = {
        "default": [
            {"url": "https://wake.example.test/first", "key_file": str(key)},
            {"url": "https://wake.example.test/second", "key_file": str(key)},
        ]
    }
    post = Post([200, 503, 200, 200, 200])
    kwargs = dict(
        runner=tools.runner,
        ledger=ledger,
        runtime=tools.runtime,
        wake_targets=targets,
        log_path=tmp_path / "wake.jsonl",
        post=post,
    )
    assert watch.run_watch_once(**kwargs)["failed"] == 1
    assert len(post.calls) == 2
    expected = {"kind": "hub-inbox", "count": 1, "latest_id": 1, "sender_ids": ["host-a"]}
    assert post.calls[0][2] == expected
    assert post.calls[1][2] == expected
    # A new process retries only the failed target, then suppresses the unchanged inbox.
    reopened = Ledger(tools.runtime.ledger_path)
    kwargs["ledger"] = reopened
    assert watch.run_watch_once(**kwargs)["woken"] == 1
    assert len(post.calls) == 3 and post.calls[-1][0].endswith("second")
    assert watch.run_watch_once(**kwargs)["woken"] == 0
    assert len(post.calls) == 3
    row["id"] = 2
    assert watch.run_watch_once(**kwargs)["woken"] == 2
    assert len(post.calls) == 5
    assert all(call["action"] == "list" for call in calls)
    assert all(0 < call["timeout"] <= 1 for call in calls)
    log = (tmp_path / "wake.jsonl").read_text()
    assert token not in log and row["body"] not in log and row["subject"] not in log
    assert token not in json.dumps([call[2] for call in post.calls])
    assert token not in "\n".join(ledger._conn.iterdump())
    reopened.close()


@pytest.mark.usefixtures("t3_owner_reads", "t3_watch_log")
def test_request_wakes_survive_hub_failure_and_use_selected_hub_target(hub, tmp_path, monkeypatch):
    from brigade.grokbot_t3 import watch
    from tests.test_grokbot_t3_watch import Post, _record

    tools, _, _, token, _, ledger = hub
    tools.runtime = replace(tools.runtime, hub_wake_target="Inbox")
    key = tmp_path / "wake.key"
    key.write_text("synthetic-wake-key")
    key.chmod(0o600)
    targets = {
        "default": [{"url": "https://wake.example.test/requests", "key_file": str(key)}],
        "Inbox": [{"url": "https://wake.example.test/inbox", "key_file": str(key)}],
    }
    post = Post([])
    kwargs = dict(
        runner=tools.runner,
        ledger=ledger,
        runtime=tools.runtime,
        wake_targets=targets,
        log_path=tmp_path / "wake.jsonl",
        post=post,
    )
    watch.run_watch_once(**kwargs)
    assert post.calls[-1][0].endswith("inbox")
    _record(ledger, "r1", state="succeeded")

    def unavailable(*args, **kwargs):
        raise FleetMessageHubError(token, code="hub_unavailable")

    monkeypatch.setattr(messages, "list_messages", unavailable)
    result = watch.run_watch_once(**kwargs)
    assert result["woken"] == 1 and result["failed"] == 1
    assert post.calls[-1][2]["request_id"] == "r1"
    assert token not in (tmp_path / "wake.jsonl").read_text()


@pytest.mark.usefixtures("t3_owner_reads", "t3_watch_log")
@pytest.mark.parametrize("elapsed_per_page,expected_pages", [(0, 3), (1.5, 2)])
def test_hub_poll_bounds_pagination_and_deadline(hub, tmp_path, monkeypatch, elapsed_per_page, expected_pages):
    from types import SimpleNamespace
    from brigade.grokbot_t3 import inbox, watch
    from tests.test_grokbot_t3_watch import Post

    tools, _, row, _, _, ledger = hub
    elapsed = 0
    calls = []

    def request(hub_url, credential, body, *, timeout=None):
        nonlocal elapsed
        calls.append(timeout)
        elapsed += elapsed_per_page
        rows = [dict(row, id=ident) for ident in range(body["after"] + 1, body["after"] + 101)]
        return 200, {"messages": rows, "next_cursor": rows[-1]["id"]}

    monkeypatch.setattr(inbox, "time", SimpleNamespace(monotonic=lambda: elapsed))
    monkeypatch.setattr(messages, "_request", request)
    key = tmp_path / "wake.key"
    key.write_text("synthetic-wake-key")
    key.chmod(0o600)
    post = Post([])
    watch.run_watch_once(
        runner=tools.runner,
        runtime=tools.runtime,
        ledger=ledger,
        post=post,
        wake_targets={"default": [{"url": "https://wake.example.test/inbox", "key_file": str(key)}]},
        log_path=tmp_path / "wake.jsonl",
    )
    assert len(calls) == expected_pages
    assert all(0 < timeout <= 1 for timeout in calls)
    assert post.calls[0][2] == {
        "kind": "hub-inbox",
        "count": 100 * expected_pages,
        "latest_id": 100 * expected_pages,
        "sender_ids": ["host-a"],
    }


@pytest.mark.usefixtures("t3_owner_reads", "t3_watch_log")
def test_overlapping_hub_wakes_cannot_regress_success_cursor(hub, tmp_path):
    from brigade.grokbot_t3 import watch
    from brigade.grokbot_t3.ledger import Ledger
    from tests.test_grokbot_t3_watch import Post

    tools, _, row, _, _, ledger = hub
    key = tmp_path / "wake.key"
    key.write_text("synthetic-wake-key")
    key.chmod(0o600)
    kwargs = dict(
        runner=tools.runner,
        runtime=tools.runtime,
        ledger=ledger,
        wake_targets={"default": [{"url": "https://wake.example.test/inbox", "key_file": str(key)}]},
        log_path=tmp_path / "wake.jsonl",
    )
    other = Ledger(tools.runtime.ledger_path)
    newer = Post([])

    def delayed_success(url, key, body):
        assert body["latest_id"] == 1
        row["id"] = 2
        assert watch.run_watch_once(**dict(kwargs, ledger=other, post=newer))["woken"] == 1
        return 200

    assert watch.run_watch_once(**kwargs, post=delayed_success)["woken"] == 1
    assert newer.calls[0][2]["latest_id"] == 2
    assert watch.run_watch_once(**kwargs, post=newer)["woken"] == 0
    assert len(newer.calls) == 1
    other.close()


def test_malformed_hub_status_is_a_closed_error(hub, monkeypatch):
    tools, _, _, _, _, _ = hub
    monkeypatch.setattr(messages, "sent", lambda **kwargs: {"messages": [None]})
    assert tools.call_tool("message_status", {"ids": [1]})["error"]["code"] == "hub-error"


def test_non_protocol_hub_code_is_omitted(hub, monkeypatch):
    tools, _, _, token, _, _ = hub

    def refused(**kwargs):
        raise FleetMessageHubError(token, status=403, code=[token])

    monkeypatch.setattr(messages, "send", refused)
    result = tools.call_tool("message_send", HUB_TOOLS["message_send"])
    assert result["error"]["code"] == "hub-error"
    assert "hub_code" not in result["error"]
    assert token not in json.dumps(result)
