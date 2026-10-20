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
    monkeypatch.setattr(
        fleet_client,
        "load_fleet_settings",
        lambda: {"hub_url": "https://hub.example.test", "node_token": "", "admin_token": ""},
    )
    monkeypatch.setattr(fleet_client, "resolve_node_id", lambda: "node-id")
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


@pytest.fixture
def hub_settings(hub, monkeypatch):
    settings = {"hub_url": "https://hub.example.test", "node_token": "", "admin_token": ""}
    monkeypatch.setattr(fleet_client, "load_fleet_settings", lambda: dict(settings))
    return settings


@pytest.fixture
def transport_settings(monkeypatch):
    """Perturb only current validation at the real transport seam, after capture."""
    from types import SimpleNamespace
    from brigade import fleet_client_message_credentials as credentials

    original_settings, original_transport = credentials.message_settings, messages._authority
    hook = SimpleNamespace(load=original_settings)
    active = False

    def settings(*args, **kwargs):
        return hook.load(*args, **kwargs) if active else original_settings(*args, **kwargs)

    def transport(*args, **kwargs):
        nonlocal active
        active = True
        try:
            return original_transport(*args, **kwargs)
        finally:
            active = False

    monkeypatch.setattr(credentials, "message_settings", settings)
    monkeypatch.setattr(messages, "_authority", transport)
    return hook


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


@pytest.mark.parametrize("change", ["token", "endpoint", "invalid-token"])
@pytest.mark.parametrize("tool_name", ["message_send", "inbox_reply", "message_status"])
@pytest.mark.parametrize("outcome", ["success", "offline", "refused"])
def test_security_message_scope_change_during_request_closes_result(
    hub, hub_settings, monkeypatch, caplog, change, tool_name, outcome
):
    tools, _, row, token, token_file, ledger = hub
    original_hub = hub_settings["hub_url"]
    replacement = "y" * 64
    path = messages._spool_path("node-id", token, hub_url=original_hub)
    requests = []

    def request(hub_url, credential, body, *, timeout=None):
        requests.append((hub_url, credential, dict(body)))
        if change == "endpoint":
            hub_settings["hub_url"] = "https://other.example.test"
        else:
            token_file.write_text("invalid" if change == "invalid-token" else replacement)
        if outcome == "offline":
            raise FleetMessageHubError(credential, code="hub_unavailable")
        if outcome == "refused":
            return 403, {"error": credential, "code": "message_recipient_not_permitted"}
        if body["action"] == "sent":
            return 200, {"messages": [row], "next_cursor": row["id"]}
        return 200, {"message": dict(row, client_id=body["client_id"])}

    monkeypatch.setattr(messages, "_request", request)
    result = tools.call_tool(tool_name, HUB_TOOLS[tool_name])
    assert result.get("error", {}).get("code") == "hub-error"
    assert result["error"].get("hub_code") == ("token_file_invalid" if change == "invalid-token" else None)
    assert set(result) == {"error"}
    assert [(hub_url, credential) for hub_url, credential, _ in requests] == [(original_hub, token)]
    pending = fleet_client._read_spool(path)
    if tool_name != "message_status" and outcome == "offline":
        assert len(pending) == 1 and pending[0]["client_id"] == requests[0][2]["client_id"]
        assert pending[0]["action"] == requests[0][2]["action"]
        # The new pair must not replay or move the original pair's durable mutation.
        if change != "invalid-token":
            assert messages.flush_credential(str(token_file)) == 0
            assert len(requests) == 1 and fleet_client._read_spool(path) == pending
        hub_settings["hub_url"] = original_hub
        token_file.write_text(token)
        delivered = []

        def restored_request(hub_url, credential, body, *, timeout=None):
            delivered.append((hub_url, credential, body["client_id"]))
            return 200, {"message": dict(row, client_id=body["client_id"])}

        monkeypatch.setattr(messages, "_request", restored_request)
        assert messages.flush_credential(str(token_file)) == 1
        assert delivered == [(original_hub, token, pending[0]["client_id"])]
        assert fleet_client._read_spool(path) == []
    else:
        assert pending == []
    public = json.dumps(result) + caplog.text + "\n".join(ledger._conn.iterdump())
    spool_text = "".join(p.read_text() for p in path.parent.glob("*.jsonl")) if path.parent.exists() else ""
    for secret in (token, replacement):
        assert secret not in public + spool_text


@pytest.mark.parametrize("change", ["token", "endpoint"])
@pytest.mark.parametrize("tool_name", ["message_send", "inbox_reply", "message_status"])
@pytest.mark.parametrize("restore_after_load", [False, True])
def test_security_message_scope_checks_loaded_transport_pair(
    hub, hub_settings, monkeypatch, caplog, change, tool_name, restore_after_load, transport_settings
):
    tools, _, row, token, token_file, ledger = hub
    original_hub = hub_settings["hub_url"]
    replacement = "y" * 64
    original_settings = transport_settings.load
    requests = []

    def settings(*args, **kwargs):
        if change == "endpoint":
            hub_settings["hub_url"] = "https://other.example.test"
        else:
            token_file.write_text(replacement)
        loaded = original_settings(*args, **kwargs)
        if restore_after_load:
            hub_settings["hub_url"] = original_hub
            token_file.write_text(token)
        return loaded

    def request(hub_url, credential, body, *, timeout=None):
        requests.append((hub_url, credential))
        if body["action"] == "sent":
            return 200, {"messages": [row], "next_cursor": row["id"]}
        return 200, {"message": dict(row, client_id=body["client_id"])}

    monkeypatch.setattr(transport_settings, "load", settings)
    monkeypatch.setattr(messages, "_request", request)
    result = tools.call_tool(tool_name, HUB_TOOLS[tool_name])
    assert result.get("error", {}).get("code") == "hub-error"
    assert set(result) == {"error"}
    assert requests == []
    directory = fleet_client.spool_path("node-id").parent
    assert not any(p.stat().st_size for p in directory.glob("messages-*.jsonl"))
    public = json.dumps(result) + caplog.text + "\n".join(ledger._conn.iterdump())
    for secret in (token, replacement):
        assert secret not in public


@pytest.mark.parametrize("change", ["token", "endpoint"])
def test_security_status_transport_failure_still_checks_current_scope(hub, hub_settings, monkeypatch, change):
    from brigade.grokbot_t3.inbox import HubInbox
    from brigade.hub_messages import HubMessageError

    _, _, _, token, token_file, _ = hub

    def request(hub_url, credential, body, *, timeout=None):
        if change == "endpoint":
            hub_settings["hub_url"] = "https://other.example.test"
        else:
            token_file.write_text("y" * 64)
        raise FleetMessageHubError(token, code="hub_unavailable")

    monkeypatch.setattr(messages, "_request", request)
    with pytest.raises(HubMessageError) as caught:
        HubInbox(token_file=str(token_file)).status([1])
    assert caught.value.code == "hub_credential_changed"


@pytest.mark.parametrize("change", ["token", "endpoint", "invalid-token"])
@pytest.mark.parametrize("fail_projection", [False, True])
@pytest.mark.parametrize(
    "tool_name,queued,rotate_at",
    [
        ("message_send", False, "sent-message"),
        ("inbox_reply", False, "sent-message"),
        ("message_send", True, "sent-message"),
        ("inbox_reply", True, "sent-message"),
        ("message_send", True, "clock"),
        ("inbox_reply", True, "clock"),
        ("message_status", False, "recipients"),
    ],
)
def test_security_message_projection_scope_change_closes_complete_operation(
    hub, hub_settings, monkeypatch, caplog, change, fail_projection, tool_name, queued, rotate_at
):
    from brigade.grokbot_t3.inbox import HubInbox
    from brigade.hub_messages import HubMessageError, MessageOperations

    tools, _, row, token, token_file, ledger = hub
    original_hub = hub_settings["hub_url"]
    replacement = "y" * 64
    requests = []
    rotations = []

    def rotate():
        # Every seam here runs after the actual transport and its final scope check.
        assert len(requests) == 1
        rotations.append(rotate_at)
        if change == "endpoint":
            hub_settings["hub_url"] = "https://other.example.test"
        else:
            token_file.write_text("invalid" if change == "invalid-token" else replacement)
        if fail_projection:
            raise HubMessageError("hub_unavailable", "message_not_found")

    def request(hub_url, credential, body, *, timeout=None):
        requests.append((hub_url, credential, dict(body)))
        if queued:
            return 503, {}
        if body["action"] == "sent":
            return 200, {"messages": [row], "next_cursor": row["id"]}
        return 200, {"message": dict(row, id=7, client_id=body["client_id"])}

    original_sent_message = HubInbox._sent_message
    original_clock = tools.now

    def sent_message(self, value, *, agent):
        result = original_sent_message(self, value, agent=agent)
        rotate()
        return result

    def clock():
        if requests:
            rotate()
        return original_clock()

    def recipients(self):
        rotate()
        return MessageOperations.recipients.fget(self)

    monkeypatch.setattr(messages, "_request", request)
    if rotate_at == "sent-message":
        monkeypatch.setattr(HubInbox, "_sent_message", sent_message)
    elif rotate_at == "clock":
        monkeypatch.setattr(tools, "now", clock)
    else:
        monkeypatch.setattr(HubInbox, "recipients", property(recipients))
    args = dict(HUB_TOOLS[tool_name])
    if tool_name == "message_send":
        args["client_id"] = "stable-id"
    result = tools.call_tool(tool_name, args)
    assert rotations == [rotate_at]
    assert result.get("error", {}).get("code") == "hub-error"
    assert result["error"].get("hub_code") == ("token_file_invalid" if change == "invalid-token" else None)
    assert set(result) == {"error"}
    assert [(hub_url, credential) for hub_url, credential, _ in requests] == [(original_hub, token)]
    path = messages._spool_path("node-id", token, hub_url=original_hub)
    pending = fleet_client._read_spool(path)
    if queued:
        assert len(pending) == 1 and pending[0]["client_id"] == requests[0][2]["client_id"]
        assert pending[0]["action"] == requests[0][2]["action"]
        if change != "invalid-token":
            assert messages.flush_credential(str(token_file)) == 0
            assert len(requests) == 1 and fleet_client._read_spool(path) == pending
    else:
        assert pending == []
    public = json.dumps(result) + caplog.text + "\n".join(ledger._conn.iterdump())
    spool_text = "".join(p.read_text() for p in path.parent.glob("*.jsonl")) if path.parent.exists() else ""
    for secret in (token, replacement):
        assert secret not in public + spool_text
    assert "stable-id" not in json.dumps(result)


@pytest.mark.parametrize("change", ["token", "endpoint", "invalid-token"])
@pytest.mark.parametrize("rotate_at", ["clock", "recipients"])
@pytest.mark.parametrize("fail_preparation", [False, True])
def test_security_send_scope_is_captured_before_clock_and_recipient_resolution(
    hub, hub_settings, monkeypatch, caplog, change, rotate_at, fail_preparation
):
    from brigade.grokbot_t3.inbox import HubInbox
    from brigade.hub_messages import HubMessageError, MessageOperations

    tools, requests, _, token, token_file, ledger = hub
    replacement = "y" * 64
    rotations = []

    def rotate():
        rotations.append(rotate_at)
        if change == "endpoint":
            hub_settings["hub_url"] = "https://other.example.test"
        else:
            token_file.write_text("invalid" if change == "invalid-token" else replacement)
        if fail_preparation:
            raise HubMessageError("hub_unavailable", "message_not_found")

    original_clock = tools.now

    def clock():
        rotate()
        return original_clock()

    def recipients(self):
        rotate()
        return MessageOperations.recipients.fget(self)

    if rotate_at == "clock":
        monkeypatch.setattr(tools, "now", clock)
    else:
        monkeypatch.setattr(HubInbox, "recipients", property(recipients))
    result = tools.call_tool("message_send", HUB_TOOLS["message_send"])
    assert rotations == [rotate_at]
    assert result.get("error", {}).get("code") == "hub-error"
    assert result["error"].get("hub_code") == ("token_file_invalid" if change == "invalid-token" else None)
    assert set(result) == {"error"} and requests == []
    public = json.dumps(result) + caplog.text + "\n".join(ledger._conn.iterdump())
    for secret in (token, replacement):
        assert secret not in public


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


@pytest.mark.usefixtures("t3_owner_reads", "t3_watch_log")
def test_review_failed_wake_does_not_starve_other_target_and_survives_ack(hub, tmp_path, monkeypatch):
    from brigade.grokbot_t3 import watch
    from brigade.grokbot_t3.inbox import HubInbox
    from brigade.grokbot_t3.ledger import Ledger, wake_target_id

    tools, _, template, token, token_file, ledger = hub
    rows = [dict(template, id=ident) for ident in range(1, 701)]
    scans = []

    def request(hub_url, credential, body, *, timeout=None):
        scans.append(body["after"])
        page = [row for row in rows if row["id"] > body["after"]][:100]
        return 200, {"messages": page, "next_cursor": page[-1]["id"] if page else body["after"]}

    monkeypatch.setattr(messages, "_request", request)
    key = tmp_path / "wake.key"
    key.write_text("synthetic-wake-key")
    key.chmod(0o600)
    targets = [{"url": f"https://wake.example.test/{name}", "key_file": str(key)} for name in ("ok", "retry")]
    delivered = []
    retry_ok = False

    def post(url, key, body):
        delivered.append((url, body))
        return 200 if url.endswith("ok") or retry_ok else 503

    kwargs = dict(
        runner=tools.runner,
        runtime=tools.runtime,
        ledger=ledger,
        wake_targets={"default": targets},
        log_path=tmp_path / "wake.jsonl",
        post=post,
    )
    scope = HubInbox(token_file=str(token_file)).notification_scope()
    for _ in range(4):
        before = len(scans)
        watch.run_watch_once(**kwargs)
        assert len(scans) - before <= 12
    healthy = [body for url, body in delivered if url.endswith("ok")]
    assert [body["latest_id"] for body in healthy] == [300, 600, 700]
    assert ledger.hub_wake_cursor(scope, wake_target_id(targets[1]["url"])) == 0
    # Persist at most one bounded batch per endpoint, without bodies or subjects.
    assert len(ledger.pending_hub_wake(scope, wake_target_id(targets[1]["url"]))) == 300
    dumped = "\n".join(ledger._conn.iterdump())
    assert token not in dumped and template["body"] not in dumped and template["subject"] not in dumped
    assert len(dumped) < 100_000
    # The Hub no longer returns the failed batch after another consumer acknowledges it.
    rows[:] = []
    ledger.close()
    reopened = Ledger(tools.runtime.ledger_path)
    kwargs["ledger"] = reopened
    retry_ok = True
    try:
        assert watch.run_watch_once(**kwargs)["woken"] == 1
        assert delivered[-1][1] == {"kind": "hub-inbox", "count": 300, "latest_id": 300, "sender_ids": ["host-a"]}
        assert reopened.hub_wake_cursor(scope, wake_target_id(targets[1]["url"])) == 300
        assert watch.run_watch_once(**kwargs)["woken"] == 0
    finally:
        reopened.close()


@pytest.mark.parametrize("body", ["x" * 8192, "é" * 4096, '\\"\n' * 2730], ids=["ascii", "multibyte", "escaped"])
def test_review_inbox_projection_fits_public_bytes_and_can_progress(hub, monkeypatch, body):
    tools, _, template, _, _, _ = hub
    rows = [dict(template, id=ident, body=body, subject="é" * 100, ref="r" * 500) for ident in range(1, 21)]

    def request(hub_url, credential, value, *, timeout=None):
        if value["action"] == "ack":
            row = next(row for row in rows if row["id"] == value["id"])
            row["acked_at"] = "2026-10-20T12:01:00+00:00"
            return 200, {"message": row}
        page = [row for row in rows if row["id"] > value["after"] and row["acked_at"] is None]
        return 200, {"messages": page, "next_cursor": page[-1]["id"] if page else value["after"]}

    monkeypatch.setattr(messages, "_request", request)
    listing = tools.call_tool("inbox_list", {})
    assert "error" not in listing
    assert len(json.dumps(listing, separators=(",", ":")).encode()) <= c.PUBLIC_RESULT_LIMIT
    ids = [row["id"] for row in listing["messages"]]
    assert 0 < len(ids) < 20 and ids == list(range(1, len(ids) + 1))
    assert listing["has_more"] is True and listing["unacked_total"] == 20
    assert all(row["body"] == body for row in listing["messages"])
    assert tools.call_tool("inbox_ack", {"ids": ids})["acked"] == ids
    assert tools.call_tool("inbox_list", {})["messages"][0]["id"] == len(ids) + 1


@pytest.mark.parametrize("pending", [20, 500])
def test_review_list_and_ack_totals_require_complete_scan(hub, monkeypatch, pending):
    tools, _, template, _, _, _ = hub
    rows = [dict(template, id=ident) for ident in range(1, pending + 1)]

    def request(hub_url, credential, body, *, timeout=None):
        if body["action"] == "ack":
            rows[0]["acked_at"] = "2026-10-20T12:01:00+00:00"
            return 200, {"message": rows[0]}
        page = [row for row in rows if row["id"] > body["after"] and row["acked_at"] is None][:100]
        return 200, {"messages": page, "next_cursor": page[-1]["id"] if page else body["after"]}

    monkeypatch.setattr(messages, "_request", request)
    listing = tools.call_tool("inbox_list", {"limit": 1})
    acked = tools.call_tool("inbox_ack", {"ids": [1]})
    assert listing["unacked_total"] == (20 if pending == 20 else None)
    assert acked["unacked_total"] == (19 if pending == 20 else None)
    assert listing["has_more"] is True and acked["has_more"] is True
    assert listing["order"] == "oldest first"
    assert listing["messages"][0]["id"] == 1
    assert acked["acked"] == [1]


def test_review_incomplete_history_labels_retained_window(hub, monkeypatch):
    tools, _, template, _, _, _ = hub

    def request(hub_url, credential, body, *, timeout=None):
        rows = [dict(template, id=ident) for ident in range(body["after"] + 1, body["after"] + 101)]
        return 200, {"messages": rows, "next_cursor": rows[-1]["id"]}

    monkeypatch.setattr(messages, "_request", request)
    listing = tools.call_tool("inbox_list", {"unacked_only": False})
    assert listing["has_more"] is True
    assert listing["messages"][0]["id"] == 300
    assert listing["order"] == "newest first within oldest fetched window"


def test_review_partial_inbox_and_ack_survive_recount_failure(hub, monkeypatch):
    tools, _, row, _, _, _ = hub

    def request(hub_url, credential, body, *, timeout=None):
        if body["action"] == "ack":
            return 200, {"message": row}
        if body["after"] == 0 and row["acked_at"] is None:
            return 200, {"messages": [row], "next_cursor": 1}
        raise FleetMessageHubError("offline", code="hub_unavailable")

    monkeypatch.setattr(messages, "_request", request)
    listing = tools.call_tool("inbox_list", {})
    assert listing["messages"][0]["id"] == 1 and listing["has_more"] is True
    row["acked_at"] = "2026-10-20T12:01:00+00:00"
    acked = tools.call_tool("inbox_ack", {"ids": [1]})
    assert acked["acked"] == [1] and acked["not_pending"] == []
    assert acked["unacked_total"] is None and acked["has_more"] is True
    assert tools.call_tool("inbox_list", {})["error"]["code"] == "hub-error"


@pytest.mark.usefixtures("t3_owner_reads", "t3_watch_log")
@pytest.mark.parametrize("enabled", [True, False])
@pytest.mark.parametrize("tool_name", ["message_send", "inbox_reply"])
def test_review_watcher_drains_queued_mutations_only_when_enabled(hub, tmp_path, monkeypatch, enabled, tool_name):
    from brigade.grokbot_t3 import watch
    from tests.test_grokbot_t3_watch import Post

    tools, _, row, _, _, ledger = hub
    online = False
    delivered = []

    def request(hub_url, credential, body, *, timeout=None):
        if body["action"] in {"send", "reply"}:
            if not online:
                return 503, {}
            delivered.append(body["client_id"])
            return 200, {"message": dict(row, client_id=body["client_id"])}
        return 200, {"messages": [], "next_cursor": body["after"]}

    monkeypatch.setattr(messages, "_request", request)
    monkeypatch.setattr(fleet_client, "load_fleet_config", lambda: {"hub_url": "https://hub.example.test"})
    sent = tools.call_tool(tool_name, HUB_TOOLS[tool_name])
    assert sent["queued"] is True
    online = True
    tools.runtime = replace(tools.runtime, enabled=enabled)
    key = tmp_path / "wake.key"
    key.write_text("synthetic-wake-key")
    key.chmod(0o600)
    watch.run_watch_once(
        runner=tools.runner,
        runtime=tools.runtime,
        ledger=ledger,
        wake_targets={"default": [{"url": "https://wake.example.test/inbox", "key_file": str(key)}]},
        log_path=tmp_path / "wake.jsonl",
        post=Post([]),
    )
    assert len(delivered) == (1 if enabled else 0)


@pytest.mark.usefixtures("t3_owner_reads", "t3_watch_log")
def test_review_flush_and_log_failures_leave_inbox_and_request_targets_working(hub, tmp_path, monkeypatch):
    from brigade.grokbot_t3 import watch
    from tests.test_grokbot_t3_watch import Post, _record

    tools, _, _, token, _, ledger = hub
    flushed = []

    def broken_flush(**kwargs):
        flushed.append(len([body for _, _, body in post.calls if "request_id" in body]) == 2)
        raise FleetMessageHubError(token, code="hub_unavailable")

    monkeypatch.setattr(messages, "flush_credential", broken_flush)
    key = tmp_path / "wake.key"
    key.write_text("synthetic-wake-key")
    key.chmod(0o600)
    _record(ledger, "r1", state="succeeded")
    post = Post([])
    targets = [{"url": f"https://wake.example.test/{n}", "key_file": str(key)} for n in ("first", "second")]
    watch.run_watch_once(
        runner=tools.runner,
        runtime=tools.runtime,
        ledger=ledger,
        wake_targets={"default": targets},
        log_path=tmp_path,
        post=post,
    )
    assert flushed == [True]
    assert [body["kind"] for _, _, body in post.calls if "kind" in body] == ["hub-inbox", "hub-inbox"]
    assert len([body for _, _, body in post.calls if "request_id" in body]) == 2


@pytest.mark.usefixtures("t3_owner_reads", "t3_watch_log")
@pytest.mark.parametrize("crash_after_post", [False, True])
def test_hub_pending_metadata_survives_crash_and_ack(hub, tmp_path, monkeypatch, crash_after_post):
    from brigade.grokbot_t3 import watch
    from brigade.grokbot_t3.inbox import HubInbox
    from brigade.grokbot_t3.ledger import Ledger, wake_target_id
    from tests.test_grokbot_t3_watch import Post

    tools, _, row, _, token_file, ledger = hub
    key = tmp_path / "wake.key"
    key.write_text("synthetic-wake-key")
    key.chmod(0o600)
    url = "https://wake.example.test/inbox"
    kwargs = dict(
        runner=tools.runner,
        runtime=tools.runtime,
        ledger=ledger,
        wake_targets={"default": [{"url": url, "key_file": str(key)}]},
        log_path=tmp_path / "wake.jsonl",
    )
    scope = HubInbox(token_file=str(token_file)).notification_scope()

    def crash(*args, **kwargs):
        raise SystemExit("synthetic watcher crash")

    post = Post([])
    if crash_after_post:
        monkeypatch.setattr(ledger, "mark_hub_notified", crash)
    with pytest.raises(SystemExit, match="synthetic watcher crash"):
        watch.run_watch_once(**kwargs, post=post if crash_after_post else crash)
    assert len(post.calls) == int(crash_after_post)
    assert ledger.hub_wake_cursor(scope, wake_target_id(url)) == 0
    ledger.close()
    row["acked_at"] = "2026-10-20T12:01:00+00:00"
    reopened = Ledger(tools.runtime.ledger_path)
    try:
        assert watch.run_watch_once(**dict(kwargs, ledger=reopened), post=post)["woken"] == 1
        assert post.calls[-1][2] == {"kind": "hub-inbox", "count": 1, "latest_id": 1, "sender_ids": ["host-a"]}
        assert reopened.hub_wake_cursor(scope, wake_target_id(url)) == 1
        assert reopened.pending_hub_wake(scope, wake_target_id(url)) == []
    finally:
        reopened.close()


def test_hub_success_cursor_and_batch_retirement_are_atomic(hub):
    import sqlite3

    _, _, _, _, _, ledger = hub
    rows = [{"id": 1, "sender": "host-a"}, {"id": 2, "sender": "host-b"}]
    ledger.queue_hub_wake("synthetic-scope", "synthetic-target", rows)
    ledger._conn.execute(
        "CREATE TEMP TRIGGER fail_retirement BEFORE DELETE ON hub_wake_pending "
        "BEGIN SELECT RAISE(ABORT, 'synthetic retirement failure'); END"
    )
    with pytest.raises(sqlite3.IntegrityError, match="synthetic retirement failure"):
        ledger.mark_hub_notified("synthetic-scope", "synthetic-target", 1)
    assert ledger.hub_wake_cursor("synthetic-scope", "synthetic-target") == 0
    assert ledger.pending_hub_wake("synthetic-scope", "synthetic-target") == rows
    ledger._conn.execute("DROP TRIGGER fail_retirement")
    ledger.mark_hub_notified("synthetic-scope", "synthetic-target", 1)
    ledger.queue_hub_wake("synthetic-scope", "synthetic-target", rows)
    assert ledger.pending_hub_wake("synthetic-scope", "synthetic-target") == rows[1:]


@pytest.mark.parametrize("rotate_at,fail_fetch", [(1, False), (2, False), (2, True)])
def test_security_rotation_discards_foreground_scan(hub, monkeypatch, caplog, rotate_at, fail_fetch):
    tools, _, row, token, token_file, ledger = hub
    replacement = "y" * 64
    fetched = 0

    def request(hub_url, credential, body, *, timeout=None):
        nonlocal fetched
        fetched += 1
        if fetched == rotate_at:
            token_file.write_text(replacement)
            if fail_fetch:
                raise FleetMessageHubError(replacement, code="hub_unavailable")
        ident = body["after"] + 1
        return 200, {"messages": [dict(row, id=ident)], "next_cursor": ident}

    monkeypatch.setattr(messages, "_request", request)
    result = tools.call_tool("inbox_list", {})
    assert result.get("error", {}).get("code") == "hub-error"
    assert fetched == rotate_at
    public = json.dumps(result) + caplog.text + "\n".join(ledger._conn.iterdump())
    assert token not in public and replacement not in public


@pytest.mark.usefixtures("t3_owner_reads", "t3_watch_log")
@pytest.mark.parametrize("rotate_at", ["first-fetch", "second-fetch", "before-persist", "before-post"])
@pytest.mark.parametrize("change", ["token", "endpoint"])
def test_security_rotation_keeps_wake_scope_consistent(
    hub, hub_settings, tmp_path, monkeypatch, caplog, rotate_at, change
):
    from brigade.grokbot_t3 import watch
    from brigade.grokbot_t3.inbox import HubInbox
    from brigade.grokbot_t3.ledger import wake_target_id
    from tests.test_grokbot_t3_watch import Post

    tools, _, row, token, token_file, ledger = hub
    replacement = "y" * 64
    scope = HubInbox(token_file=str(token_file)).notification_scope()
    key = tmp_path / "wake.key"
    key.write_text("synthetic-wake-key")
    key.chmod(0o600)
    url = "https://wake.example.test/inbox"
    target_id = wake_target_id(url)
    fetched = 0

    def rotate():
        if change == "token":
            token_file.write_text(replacement)
        else:
            hub_settings["hub_url"] = "https://other.example.test"

    def request(hub_url, credential, body, *, timeout=None):
        nonlocal fetched
        fetched += 1
        if fetched == (1 if rotate_at == "first-fetch" else 2) and rotate_at.endswith("fetch"):
            rotate()
        ident = body["after"] + 1
        sender = "host-b" if credential == replacement or hub_url == "https://other.example.test" else row["sender"]
        return 200, {"messages": [dict(row, id=ident, sender=sender)], "next_cursor": ident}

    if rotate_at == "before-persist":
        original_pages = HubInbox.pages

        def pages(self, **kwargs):
            for page in original_pages(self, **kwargs):
                rotate()
                yield page

        monkeypatch.setattr(HubInbox, "pages", pages)
    if rotate_at == "before-post":
        original_read_key = watch.read_key

        def read_key(path):
            value = original_read_key(path)
            rotate()
            return value

        monkeypatch.setattr(watch, "read_key", read_key)
    monkeypatch.setattr(messages, "_request", request)
    post = Post([])
    kwargs = dict(
        runner=tools.runner,
        runtime=tools.runtime,
        ledger=ledger,
        post=post,
        wake_targets={"default": [{"url": url, "key_file": str(key)}]},
        log_path=tmp_path / "wake.jsonl",
    )
    watch.run_watch_once(**kwargs)
    assert post.calls == []
    assert ledger.hub_wake_cursor(scope, target_id) == 0
    pending = ledger.pending_hub_wake(scope, target_id)
    if rotate_at in {"first-fetch", "before-persist"}:
        assert pending == []
    else:
        assert pending and all(item["sender"] == "host-a" for item in pending)
    # The next tick starts a fresh scope and leaves the old scope pending for retry.
    new_scope = HubInbox(token_file=str(token_file)).notification_scope()
    assert new_scope != scope
    assert watch.run_watch_once(**kwargs)["woken"] == 1
    assert post.calls[0][2]["sender_ids"] == ["host-b"]
    assert ledger.hub_wake_cursor(new_scope, target_id) > 0
    assert ledger.hub_wake_cursor(scope, target_id) == 0
    public = caplog.text + (tmp_path / "wake.jsonl").read_text() + "\n".join(ledger._conn.iterdump())
    assert token not in public and replacement not in public


@pytest.mark.parametrize("failure", ["deadline", "timeout", "grant"])
def test_security_ack_deadline_preserves_confirmed_results(hub, monkeypatch, caplog, failure):
    from types import SimpleNamespace
    from brigade.grokbot_t3 import inbox

    tools, _, row, token, _, ledger = hub
    elapsed = 0.0
    calls = []

    def request(hub_url, credential, body, *, timeout=None):
        nonlocal elapsed
        calls.append((dict(body), timeout))
        if body["action"] == "list":
            return 200, {"messages": [], "next_cursor": body["after"]}
        elapsed += min(4.0, timeout or 4.0)
        if body.get("id") == 2:
            raise FleetMessageHubError(token, status=404, code="message_not_found")
        if body.get("id") == 3 and failure != "deadline":
            raise FleetMessageHubError(token, status=403 if failure == "grant" else None, code="hub_unavailable")
        return 200, {"message": dict(row, id=body["id"])}

    monkeypatch.setattr(inbox, "time", SimpleNamespace(monotonic=lambda: elapsed))
    monkeypatch.setattr(messages, "_request", request)
    result = tools.call_tool("inbox_ack", {"ids": [1, 2, 3, 4]})
    assert result.get("acked") == ([1, 3] if failure == "deadline" else [1])
    assert result["not_pending"] == [2]
    assert result["incomplete"] is True
    assert result["remaining_ids"] == ([4] if failure == "deadline" else [3, 4])
    assert result["unacked_total"] is None and result["has_more"] is True
    assert [body["id"] for body, _ in calls] == [1, 2, 3]
    assert [timeout for _, timeout in calls] == [5.0, 5.0, 2.0]
    public = json.dumps(result) + caplog.text + "\n".join(ledger._conn.iterdump())
    assert token not in public


@pytest.mark.parametrize("setup_delay", [0.0, 0.25])
def test_security_ack_recount_shares_batch_deadline(hub, monkeypatch, setup_delay):
    from types import SimpleNamespace
    from brigade.grokbot_t3 import inbox

    tools, _, row, _, _, _ = hub
    elapsed = 0.0
    calls = []
    original_scope = inbox.HubInbox.capture
    scope_reads = 0

    def scope(self):
        nonlocal elapsed, scope_reads
        if scope_reads == 0:
            elapsed += setup_delay
        scope_reads += 1
        return original_scope(self)

    def request(hub_url, credential, body, *, timeout=None):
        nonlocal elapsed
        calls.append((body["action"], timeout))
        if body["action"] == "ack":
            elapsed += 4.75
            return 200, {"message": dict(row, id=body["id"])}
        assert timeout is not None and timeout <= 10.0 - elapsed
        elapsed += timeout
        return 200, {"messages": [row], "next_cursor": row["id"]}

    monkeypatch.setattr(inbox.HubInbox, "capture", scope)
    monkeypatch.setattr(inbox, "time", SimpleNamespace(monotonic=lambda: elapsed))
    monkeypatch.setattr(messages, "_request", request)
    result = tools.call_tool("inbox_ack", {"ids": [1, 2]})
    assert result["acked"] == [1, 2]
    assert result["has_more"] is True
    assert calls == [("ack", 5.0), ("ack", 5.0), ("list", 0.5 - setup_delay)]
    assert elapsed == 10.0


@pytest.mark.parametrize("status", [401, 403])
def test_security_first_ack_refusal_stays_closed(hub, monkeypatch, status):
    tools, _, _, token, _, _ = hub

    def refused(*args, **kwargs):
        raise FleetMessageHubError(token, status=status, code="message_recipient_not_permitted")

    monkeypatch.setattr(messages, "ack", refused)
    result = tools.call_tool("inbox_ack", {"ids": [1, 2]})
    assert result["error"]["code"] == "hub-error"
    assert "acked" not in result and token not in json.dumps(result)


@pytest.mark.parametrize("invalid", [False, True])
@pytest.mark.parametrize("rotate_at,outcome", [(1, "success"), (2, "success"), (2, "missing"), (2, "offline")])
def test_security_ack_rotation_closes_batch_after_request(hub, monkeypatch, caplog, invalid, rotate_at, outcome):
    import hashlib

    tools, _, row, token, token_file, ledger = hub
    replacement = "y" * 64
    requests = []

    def request(hub_url, credential, body, *, timeout=None):
        requests.append((credential, dict(body)))
        if body["action"] == "ack":
            if body["id"] == rotate_at:
                token_file.write_text("invalid" if invalid else replacement)
                if outcome != "success":
                    raise FleetMessageHubError(
                        replacement, status=404 if outcome == "missing" else None, code="hub_unavailable"
                    )
            return 200, {"message": dict(row, id=body["id"])}
        return 200, {"messages": [], "next_cursor": body["after"]}

    monkeypatch.setattr(messages, "_request", request)
    result = tools.call_tool("inbox_ack", {"ids": [1, 2, 3]})
    assert result.get("error", {}).get("code") == "hub-error"
    assert "acked" not in result and "not_pending" not in result
    assert [(credential, body["id"]) for credential, body in requests] == [
        (token, ident) for ident in range(1, rotate_at + 1)
    ]
    public = json.dumps(result) + caplog.text + "\n".join(ledger._conn.iterdump())
    for secret in (token, replacement):
        assert secret not in public and hashlib.sha256(secret.encode()).hexdigest() not in public


@pytest.mark.parametrize("invalid,restore_after_load", [(False, False), (False, True), (True, False)])
@pytest.mark.parametrize("rotate_before", ["first-ack", "later-ack", "recount"])
def test_security_ack_checks_loaded_transport_credential(
    hub, monkeypatch, caplog, invalid, restore_after_load, rotate_before, transport_settings
):
    import hashlib

    tools, _, row, token, token_file, ledger = hub
    replacement = "y" * 64
    original_settings = transport_settings.load
    loads = 0
    requests = []
    rotate_on = {"first-ack": 1, "later-ack": 2, "recount": 3}[rotate_before]

    def settings(*args, **kwargs):
        nonlocal loads
        loads += 1
        # Rotation occurs after the connector's outer guard, before transport loads it.
        if loads == rotate_on:
            token_file.write_text("invalid" if invalid else replacement)
        loaded = original_settings(*args, **kwargs)
        if loads == rotate_on and restore_after_load:
            # Even a rotation back before the outer guard must not send the other token.
            token_file.write_text(token)
        return loaded

    def request(hub_url, credential, body, *, timeout=None):
        requests.append((credential, dict(body)))
        if body["action"] == "ack":
            return 200, {"message": dict(row, id=body["id"])}
        return 200, {"messages": [], "next_cursor": body["after"]}

    monkeypatch.setattr(transport_settings, "load", settings)
    monkeypatch.setattr(messages, "_request", request)
    result = tools.call_tool("inbox_ack", {"ids": [1, 2]})
    assert result.get("error", {}).get("code") == "hub-error"
    assert "acked" not in result
    assert [(credential, body["id"]) for credential, body in requests] == [
        (token, ident) for ident in range(1, rotate_on)
    ]
    public = json.dumps(result) + caplog.text + "\n".join(ledger._conn.iterdump())
    for secret in (token, replacement):
        assert secret not in public and hashlib.sha256(secret.encode()).hexdigest() not in public


@pytest.mark.parametrize("fail_recount", [False, True])
def test_security_ack_rotation_during_recount_closes_result(hub, monkeypatch, caplog, fail_recount):
    tools, _, row, token, token_file, ledger = hub
    replacement = "y" * 64
    requests = []

    def request(hub_url, credential, body, *, timeout=None):
        requests.append((credential, dict(body)))
        if body["action"] == "ack":
            return 200, {"message": row}
        token_file.write_text(replacement)
        if fail_recount:
            raise FleetMessageHubError(replacement, code="hub_unavailable")
        return 200, {"messages": [dict(row, body=replacement)], "next_cursor": 1}

    monkeypatch.setattr(messages, "_request", request)
    result = tools.call_tool("inbox_ack", {"ids": [1]})
    assert result.get("error", {}).get("code") == "hub-error"
    assert "acked" not in result and "unacked_total" not in result
    assert [(credential, body["action"]) for credential, body in requests] == [(token, "ack"), (token, "list")]
    public = json.dumps(result) + caplog.text + "\n".join(ledger._conn.iterdump())
    assert token not in public and replacement not in public


def test_notification_scope_matches_transport_endpoint_and_frames_identity(hub, hub_settings):
    from brigade.grokbot_t3.inbox import HubInbox

    _, _, _, token, token_file, _ = hub
    inbox = HubInbox(token_file=str(token_file))
    scope = inbox.notification_scope()
    hub_settings["hub_url"] += "///"
    assert inbox.notification_scope() == scope
    hub_settings["hub_url"] = "https://other.example.test"
    assert inbox.notification_scope() != scope
    # These pairs collide under plain concatenation, but identify different transports.
    hub_settings["hub_url"] = "https://hub.example.test/a"
    token_file.write_text("b" * 64)
    first = inbox.notification_scope()
    hub_settings["hub_url"] += "b"
    token_file.write_text("b" * 63)
    assert inbox.notification_scope() != first
    token_file.write_text(token)


@pytest.mark.parametrize("failure", ["missing-hub", "settings-error"])
def test_notification_scope_settings_failure_is_closed(hub, hub_settings, monkeypatch, failure):
    from brigade.grokbot_t3.inbox import HubInbox
    from brigade.hub_messages import HubMessageError

    _, _, _, token, token_file, _ = hub
    hub_settings["hub_url"] = ""
    if failure == "settings-error":

        def broken_settings():
            raise fleet_client.FleetClientError(token)

        monkeypatch.setattr(fleet_client, "load_fleet_settings", broken_settings)
    with pytest.raises(HubMessageError) as caught:
        HubInbox(token_file=str(token_file)).notification_scope()
    assert str(caught.value) == "hub_credential_invalid"
    assert token not in str(caught.value)


@pytest.mark.parametrize("switch_at,outcome", [(1, "success"), (2, "success"), (2, "missing"), (2, "offline")])
def test_security_ack_endpoint_switch_closes_batch(hub, hub_settings, monkeypatch, caplog, switch_at, outcome):
    tools, _, row, token, _, ledger = hub
    requests = []
    original_hub = hub_settings["hub_url"]

    def request(hub_url, credential, body, *, timeout=None):
        requests.append((hub_url, dict(body)))
        if body["action"] == "ack":
            if body["id"] == switch_at:
                hub_settings["hub_url"] = "https://other.example.test"
                if outcome != "success":
                    raise FleetMessageHubError(
                        token, status=404 if outcome == "missing" else None, code="hub_unavailable"
                    )
            return 200, {"message": dict(row, id=body["id"])}
        return 200, {"messages": [], "next_cursor": body["after"]}

    monkeypatch.setattr(messages, "_request", request)
    result = tools.call_tool("inbox_ack", {"ids": [1, 2, 3]})
    assert result.get("error", {}).get("code") == "hub-error"
    assert "acked" not in result and "not_pending" not in result
    assert [(hub_url, body["id"]) for hub_url, body in requests] == [
        (original_hub, ident) for ident in range(1, switch_at + 1)
    ]
    assert token not in json.dumps(result) + caplog.text + "\n".join(ledger._conn.iterdump())


@pytest.mark.parametrize("restore_after_load", [False, True])
@pytest.mark.parametrize(
    "tool,load_at", [("inbox_ack", 1), ("inbox_ack", 2), ("inbox_ack", 3), ("inbox_list", 1), ("inbox_list", 2)]
)
def test_security_inbox_checks_loaded_endpoint(
    hub, hub_settings, monkeypatch, caplog, restore_after_load, tool, load_at, transport_settings
):
    tools, _, row, token, _, ledger = hub
    original_settings = transport_settings.load
    original_hub = hub_settings["hub_url"]
    loads = 0
    requests = []

    def settings(*args, **kwargs):
        nonlocal loads
        loads += 1
        if loads == load_at:
            hub_settings["hub_url"] = "https://other.example.test"
        loaded = original_settings(*args, **kwargs)
        if loads == load_at and restore_after_load:
            hub_settings["hub_url"] = original_hub
        return loaded

    def request(hub_url, credential, body, *, timeout=None):
        requests.append((hub_url, dict(body)))
        if body["action"] == "ack":
            return 200, {"message": dict(row, id=body["id"])}
        ident = body["after"] + 1
        return 200, {"messages": [dict(row, id=ident)], "next_cursor": ident}

    monkeypatch.setattr(transport_settings, "load", settings)
    monkeypatch.setattr(messages, "_request", request)
    result = tools.call_tool(tool, {"ids": [1, 2]} if tool == "inbox_ack" else {})
    assert result.get("error", {}).get("code") == "hub-error"
    assert "acked" not in result and "messages" not in result
    assert [hub_url for hub_url, _ in requests] == [original_hub] * (load_at - 1)
    assert token not in json.dumps(result) + caplog.text + "\n".join(ledger._conn.iterdump())


@pytest.mark.parametrize("fail_recount", [False, True])
def test_security_ack_endpoint_switch_during_recount(hub, hub_settings, monkeypatch, caplog, fail_recount):
    tools, _, row, token, _, ledger = hub
    requests = []
    original_hub = hub_settings["hub_url"]

    def request(hub_url, credential, body, *, timeout=None):
        requests.append((hub_url, body["action"]))
        if body["action"] == "ack":
            return 200, {"message": row}
        hub_settings["hub_url"] = "https://other.example.test"
        if fail_recount:
            raise FleetMessageHubError(token, code="hub_unavailable")
        return 200, {"messages": [row], "next_cursor": 1}

    monkeypatch.setattr(messages, "_request", request)
    result = tools.call_tool("inbox_ack", {"ids": [1]})
    assert result.get("error", {}).get("code") == "hub-error"
    assert "acked" not in result and "unacked_total" not in result
    assert requests == [(original_hub, "ack"), (original_hub, "list")]
    assert token not in json.dumps(result) + caplog.text + "\n".join(ledger._conn.iterdump())


@pytest.mark.parametrize("switch_at,fail_fetch", [(1, False), (2, False), (2, True)])
def test_security_endpoint_switch_discards_list_pages(hub, hub_settings, monkeypatch, switch_at, fail_fetch):
    tools, _, row, _, _, _ = hub
    fetched = 0

    def request(hub_url, credential, body, *, timeout=None):
        nonlocal fetched
        fetched += 1
        if fetched == switch_at:
            hub_settings["hub_url"] = "https://other.example.test"
            if fail_fetch:
                raise FleetMessageHubError("offline", code="hub_unavailable")
        ident = body["after"] + 1
        return 200, {"messages": [dict(row, id=ident)], "next_cursor": ident}

    monkeypatch.setattr(messages, "_request", request)
    result = tools.call_tool("inbox_list", {})
    assert result.get("error", {}).get("code") == "hub-error"
    assert "messages" not in result and fetched == switch_at


@pytest.mark.usefixtures("t3_owner_reads", "t3_watch_log")
@pytest.mark.parametrize("history,new_id", [("endpoint", 1), ("endpoint", 100), ("legacy-token-only", 1)])
def test_hub_wake_endpoint_and_legacy_histories_are_isolated(hub, hub_settings, tmp_path, caplog, history, new_id):
    import hashlib
    from brigade.grokbot_t3 import watch
    from brigade.grokbot_t3.inbox import HubInbox
    from brigade.grokbot_t3.ledger import Ledger, wake_target_id
    from tests.test_grokbot_t3_watch import Post

    tools, calls, row, token, token_file, ledger = hub
    key = tmp_path / "wake.key"
    key.write_text("synthetic-wake-key")
    key.chmod(0o600)
    url = "https://wake.example.test/inbox"
    target = wake_target_id(url)
    inbox = HubInbox(token_file=str(token_file))
    old_scope = (
        hashlib.sha256(token.encode()).hexdigest() if history == "legacy-token-only" else inbox.notification_scope()
    )
    ledger.mark_hub_notified(old_scope, target, 100)
    old_pending = [{"id": 101, "sender": "old-sender"}]
    ledger.queue_hub_wake(old_scope, target, old_pending)
    ledger.close()
    if history == "endpoint":
        hub_settings["hub_url"] = "https://other.example.test"
    row.update(id=new_id, sender="new-sender")
    new_scope = inbox.notification_scope()
    post = Post([503, 200])
    reopened = Ledger(tools.runtime.ledger_path)
    try:
        kwargs = dict(
            runner=tools.runner,
            runtime=tools.runtime,
            ledger=reopened,
            wake_targets={"default": [{"url": url, "key_file": str(key)}]},
            log_path=tmp_path / "wake.jsonl",
            post=post,
        )
        assert watch.run_watch_once(**kwargs)["failed"] == 1
        expected = {"kind": "hub-inbox", "count": 1, "latest_id": new_id, "sender_ids": ["new-sender"]}
        assert post.calls[0][2] == expected
        assert calls[0]["after"] == 0
        assert new_scope != old_scope
        assert reopened.hub_wake_cursor(new_scope, target) == 0
        assert reopened.pending_hub_wake(new_scope, target) == [{"id": new_id, "sender": "new-sender"}]
        assert watch.run_watch_once(**kwargs)["woken"] == 1
        assert post.calls[1][2] == expected
        assert reopened.hub_wake_cursor(new_scope, target) == new_id
        assert reopened.pending_hub_wake(new_scope, target) == []
        assert reopened.hub_wake_cursor(old_scope, target) == 100
        assert reopened.pending_hub_wake(old_scope, target) == old_pending
        assert watch.run_watch_once(**kwargs)["woken"] == 0
        assert len(post.calls) == 2
        public = caplog.text + (tmp_path / "wake.jsonl").read_text() + "\n".join(reopened._conn.iterdump())
        assert token not in public and row["body"] not in public and row["subject"] not in public
        assert token not in json.dumps([body for _, _, body in post.calls])
    finally:
        reopened.close()


def test_pages_capture_at_invocation_before_iteration(hub, monkeypatch):
    from brigade.grokbot_t3.inbox import HubInbox
    from brigade.hub_messages import HubMessageError

    _, calls, _, _, token_file, _ = hub

    def request(hub, token, body, **kwargs):
        calls.append(dict(body))
        return 200, {"messages": [], "next_cursor": body["after"]}

    monkeypatch.setattr(messages, "_request", request)
    pages = HubInbox(token_file=str(token_file)).pages()
    token_file.write_text("y" * 64)
    with pytest.raises(HubMessageError, match="hub_credential_changed"):
        next(pages)
    assert calls == []


@pytest.mark.usefixtures("t3_owner_reads", "t3_watch_log")
@pytest.mark.parametrize("stage", ["fetch", "pre-post"])
@pytest.mark.parametrize("failure", ["changed", "invalid"])
def test_transient_authority_failure_ends_hub_wake_invocation_but_not_request_wakes(
    hub, tmp_path, monkeypatch, stage, failure
):
    from brigade import fleet_client_message_credentials as credentials
    from brigade.grokbot_t3 import watch
    from brigade.grokbot_t3.inbox import HubInbox
    from brigade.grokbot_t3.ledger import wake_target_id
    from tests.test_grokbot_t3_watch import Post, _record

    tools, _, row, _, token_file, ledger = hub
    tools.runtime = replace(tools.runtime, hub_wake_target="Hub")
    scope = HubInbox(token_file=str(token_file)).notification_scope()
    key = tmp_path / "wake.key"
    key.write_text("synthetic-wake-key")
    key.chmod(0o600)
    targets = [{"url": "https://wake.example.test/" + suffix, "key_file": str(key)} for suffix in ("first", "second")]
    for target in targets:
        ledger.queue_hub_wake(scope, wake_target_id(target["url"]), [row])
    _record(ledger, "request-wake", state="succeeded")
    original_settings, original_read_key = credentials.message_settings, watch.read_key
    armed, fired = False, False

    def settings(*args, **kwargs):
        nonlocal armed
        value = original_settings(*args, **kwargs)
        if armed:
            armed = False  # The next read is restored A, but this operation must stay closed.
            if failure == "invalid":
                raise FleetMessageHubError("synthetic invalid credential", code="token_file_invalid")
            return value[0], "y" * 64, value[2]
        return value

    def request(endpoint, token, body, **kwargs):
        nonlocal armed, fired
        if stage == "fetch" and not fired:
            armed = fired = True
        return 200, {"messages": [], "next_cursor": body["after"]}

    def read_key(path):
        nonlocal armed, fired
        value = original_read_key(path)
        if stage == "pre-post" and not fired:
            armed = fired = True
        return value

    monkeypatch.setattr(credentials, "message_settings", settings)
    monkeypatch.setattr(messages, "_request", request)
    monkeypatch.setattr(watch, "read_key", read_key)
    post = Post([])
    kwargs = dict(
        runner=tools.runner,
        runtime=tools.runtime,
        ledger=ledger,
        post=post,
        wake_targets={"Hub": targets, "Builder": [{"url": "https://wake.example.test/request", "key_file": str(key)}]},
        log_path=tmp_path / "wake.jsonl",
    )
    summary = watch.run_watch_once(**kwargs)
    assert fired and summary["failed"] >= 1
    assert [call[2].get("request_id") for call in post.calls] == ["request-wake"]
    for target in targets:
        target_id = wake_target_id(target["url"])
        assert ledger.hub_wake_cursor(scope, target_id) == 0
        assert ledger.pending_hub_wake(scope, target_id) == [{"id": row["id"], "sender": row["sender"]}]
    assert watch.run_watch_once(**kwargs)["woken"] == 2
    assert [call[2].get("kind") for call in post.calls[1:]] == ["hub-inbox", "hub-inbox"]
