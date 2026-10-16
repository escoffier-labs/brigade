"""Protected external credentials and the bridge-facing message CLI contract."""

import io
import json
import os

import pytest

from brigade import cli, fleet_client, fleet_client_messages as client
from brigade import fleet_client_message_credentials as credentials
from tests.test_fleet_messages import NODE_A, NODE_B
from tests.test_fleet_message_grants import enroll

pytest_plugins = ["tests.test_fleet_messages", "tests.test_fleet_message_grants"]


def token_file(tmp_path, name="agent", value=None):
    path = tmp_path / name
    path.write_text(value if value is not None else "fixture_credential_" + name * 32)
    path.chmod(0o600)
    return path


@pytest.fixture
def configured(tmp_path, monkeypatch):
    monkeypatch.setenv("BRIGADE_HOME", str(tmp_path / "state"))
    monkeypatch.delenv(credentials.ENV_TOKEN_FILE, raising=False)
    monkeypatch.setattr(
        fleet_client,
        "load_fleet_settings",
        lambda: {"hub_url": "https://hub.invalid", "node_token": "fixture-node", "admin_token": "fixture-admin"},
    )
    monkeypatch.setattr(fleet_client, "resolve_node_id", lambda: NODE_A)


def test_credential_precedence_and_spool_scope(configured, tmp_path, monkeypatch):
    explicit = token_file(tmp_path, "explicit")
    environment = token_file(tmp_path, "environment")
    node = credentials.message_settings(None)
    monkeypatch.setenv(credentials.ENV_TOKEN_FILE, str(environment))
    env = credentials.message_settings(None)
    flag = credentials.message_settings(str(explicit))
    # Assert only boolean comparisons, so failures never disclose credentials.
    node_matches = node[1] == "fixture-node"
    env_matches = env[1] == environment.read_text()
    flag_matches = flag[1] == explicit.read_text()
    assert node_matches is True
    assert env_matches is True
    assert flag_matches is True
    assert client._spool_path(NODE_A, flag[1]) != client._spool_path(NODE_A, env[1])


@pytest.mark.parametrize(
    "invalid", ["symlink", "mode", "owner", "oversize", "charset", "newline", "directory", "fifo", "admin"]
)
def test_invalid_credential_files_are_secret_safe(configured, tmp_path, monkeypatch, caplog, invalid):
    sentinel = "SENTINEL_SECRET_NEVER_ECHO_" * 2
    path = token_file(tmp_path, value=sentinel)
    if invalid == "symlink":
        link = tmp_path / "link"
        link.symlink_to(path)
        path = link
    elif invalid == "mode":
        path.chmod(0o644)
    elif invalid == "owner":
        owner = os.geteuid()
        monkeypatch.setattr(os, "geteuid", lambda: owner + 1)
    elif invalid == "oversize":
        path.write_text(sentinel + "x" * 4096)
    elif invalid == "charset":
        path.write_text(sentinel + "!")
    elif invalid == "newline":
        path.write_text(sentinel + "\n")
    elif invalid == "directory":
        path.unlink()
        path.mkdir(mode=0o700)
    elif invalid == "fifo":
        path.unlink()
        os.mkfifo(path, 0o600)
    elif invalid == "admin":
        monkeypatch.setattr(
            fleet_client,
            "load_fleet_settings",
            lambda: {"hub_url": "https://hub.invalid", "node_token": "", "admin_token": sentinel},
        )
    with pytest.raises(fleet_client.FleetClientError) as error:
        credentials.message_settings(str(path))
    leaked_error = sentinel in str(error.value)
    leaked_log = sentinel in caplog.text
    assert leaked_error is False
    assert leaked_log is False


@pytest.mark.parametrize("mode", ["error", "unknown"])
def test_token_file_without_node_identity(configured, tmp_path, monkeypatch, mode):
    path = token_file(tmp_path)
    monkeypatch.setattr(
        fleet_client,
        "load_fleet_settings",
        lambda: {"hub_url": "https://hub.invalid", "node_token": "", "admin_token": ""},
    )

    def no_identity():
        if mode == "unknown":
            return "unknown"
        raise fleet_client.FleetClientError("no node identity")

    monkeypatch.setattr(fleet_client, "resolve_node_id", no_identity)
    assert credentials.message_settings(str(path))[2] == "message-principal"
    monkeypatch.setattr(client, "_request", lambda *_args, **_kwargs: (200, {"messages": [], "next_cursor": 0}))
    assert client.list_messages(token_file=str(path)) == {"messages": [], "next_cursor": 0}


def test_cli_json_denial_and_stdin_retry(hub, tmp_path, monkeypatch, capsys):
    base, _, _, _ = hub
    principal = enroll(base)
    path = token_file(tmp_path, value=principal["token"])
    monkeypatch.setenv("BRIGADE_HOME", str(tmp_path / "state"))
    monkeypatch.setattr(
        fleet_client, "load_fleet_settings", lambda: {"hub_url": base, "node_token": "", "admin_token": ""}
    )
    monkeypatch.setattr(fleet_client, "resolve_node_id", lambda: NODE_A)
    argv = [
        "fleet",
        "msg",
        "send",
        "--to",
        NODE_B,
        "--subject",
        "Task",
        "--body-file",
        "-",
        "--client-id",
        "stable",
        "--token-file",
        str(path),
        "--json",
    ]
    monkeypatch.setattr("sys.stdin", io.TextIOWrapper(io.BytesIO(b"Work")))
    assert cli.main(argv) == 1
    output = capsys.readouterr()
    assert json.loads(output.out)["status"] == 403
    assert json.loads(output.out)["code"] == "message_recipient_not_permitted"
    assert "error:" in output.err
    from tests.test_fleet_message_grants import set_grants

    set_grants(base, [NODE_B])
    ids = []
    for _ in range(2):
        monkeypatch.setattr("sys.stdin", io.TextIOWrapper(io.BytesIO(b"Work")))
        assert cli.main(argv + ["--thread", "issue:1", "--kind", "question"]) == 0
        result = json.loads(capsys.readouterr().out)["message"]
        assert result["thread"] == "issue:1"
        ids.append(result["id"])
    assert ids[0] == ids[1]
    assert cli.main(["fleet", "msg", "sent", "--id", str(ids[0]), "--token-file", str(path), "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["messages"][0]["id"] == ids[0]


@pytest.mark.parametrize("client_id", ["", "a/b", "a" * 65, "bad key"])
def test_client_id_validation(configured, tmp_path, client_id):
    with pytest.raises(fleet_client.FleetClientError, match="client_id"):
        client.send(
            recipient=NODE_B,
            subject="Task",
            body="Work",
            reference=None,
            priority="normal",
            client_id=client_id,
            token_file=str(token_file(tmp_path)),
        )


def test_flush_credential_and_empty_spool_warnings(configured, tmp_path, monkeypatch, caplog, capsys):
    path = token_file(tmp_path)
    monkeypatch.setattr(
        client, "_request", lambda *_args, **_kwargs: (_ for _ in ()).throw(fleet_client.FleetClientError("offline"))
    )
    queued = client.send(
        recipient=NODE_B,
        subject="Task",
        body="Work",
        reference=None,
        priority="normal",
        client_id="flush-key",
        token_file=str(path),
    )
    assert queued["queued"] is True

    def deliver(_hub, _token, body, **_kwargs):
        return 201, {"message": {"id": 42, "client_id": body["client_id"]}}

    monkeypatch.setattr(client, "_request", deliver)
    assert cli.main(["fleet", "msg", "flush", "--token-file", str(path), "--json"]) == 0
    assert json.loads(capsys.readouterr().out) == {"delivered": 1}
    empty = client._spool_path(NODE_A, "empty-fixture")
    empty.touch(mode=0o600)
    caplog.clear()
    client._warn_inactive_spools(NODE_A, client._spool_path(NODE_A, "active-fixture"))
    assert "other credentials remain queued" not in caplog.text


@pytest.mark.parametrize(
    "response",
    [
        {"messages": [{"id": 2}, {"id": 1}], "next_cursor": 1},
        {"messages": [], "next_cursor": True},
        {"messages": [{"id": 5}], "next_cursor": 5},
    ],
)
def test_sent_client_rejects_bad_pages(configured, monkeypatch, response):
    monkeypatch.setattr(client, "_request", lambda *_args, **_kwargs: (200, response))
    with pytest.raises(fleet_client.FleetClientError, match="page"):
        client.sent(ids=[1, 2])


def test_pending_enrollment_preserves_grants(configured, monkeypatch):
    monkeypatch.setattr(client, "_principal_enrollment_generation", lambda *_args: None)
    calls = []

    def unavailable(_hub, _token, body):
        calls.append(dict(body))
        raise fleet_client.FleetClientError("offline")

    monkeypatch.setattr(client, "_principal_admin_request", unavailable)
    for patterns in ([NODE_A], [NODE_B]):
        with pytest.raises(fleet_client.FleetClientError, match="offline"):
            client.manage_principal("add", name="fixture", may_send_to=patterns)
    assert [body["may_send_to"] for body in calls] == [[NODE_A], [NODE_A]]
    assert calls[0]["request_id"] == calls[1]["request_id"]


def test_client_grants_and_show_use_admin(hub, tmp_path, monkeypatch):
    from tests.test_fleet_messages import ADMIN

    base, _, _, _ = hub
    enroll(base)
    monkeypatch.setenv("BRIGADE_HOME", str(tmp_path / "state"))
    monkeypatch.setattr(
        fleet_client, "load_fleet_settings", lambda: {"hub_url": base, "node_token": "", "admin_token": ADMIN}
    )
    assert client.set_principal_grants("fixture", [NODE_A])["principal"]["may_send_to"] == [NODE_A]
    assert client.show_principal("fixture")["principal"]["may_send_to"] == [NODE_A]


def test_sender_recipient_quota_does_not_block_other_recipients(configured, monkeypatch, tmp_path):
    path = client._spool_path(NODE_A, "fixture-node")
    from datetime import datetime, timezone

    records = [
        {
            "action": "send",
            "to": to,
            "subject": "Task",
            "body": "Work",
            "client_id": key,
            "enqueued_at": datetime.now(timezone.utc).isoformat(),
        }
        for to, key in [(NODE_A, "one"), (NODE_A, "two"), (NODE_B, "three")]
    ]
    with fleet_client._spool_lock(path) as fd:
        fleet_client._write_spool_atomic(path, records, dir_fd=fd)
    attempted = []

    def deliver(_hub, _token, body, **_kwargs):
        attempted.append(body["client_id"])
        if body["to"] == NODE_A:
            return 429, {"code": "message_sender_recipient_quota", "recipient": NODE_A}
        return 201, {"message": {"id": 42, "client_id": body["client_id"]}}

    monkeypatch.setattr(client, "_request", deliver)
    assert client.flush_credential() == 1
    assert attempted == ["one", "three"]
    assert [row["client_id"] for row in fleet_client._read_spool(path)] == ["one", "two"]


def test_stdin_body_limit(configured, monkeypatch, capsys):
    monkeypatch.setattr("sys.stdin", io.TextIOWrapper(io.BytesIO(b"x" * (65536 + 1))))
    assert cli.main(["fleet", "msg", "send", "--to", NODE_A, "--subject", "Task", "--body-file", "-", "--json"]) == 1
    assert json.loads(capsys.readouterr().out)["status"] is None


def test_cli_agent_reply_list_and_ack(hub, tmp_path, monkeypatch, capsys):
    from tests.test_fleet_message_grants import send

    base, token_a, _, _ = hub
    principal = enroll(base)
    parent = send(base, token_a, "agent:fixture", thread="issue:44")[1]["message"]
    path = token_file(tmp_path, value=principal["token"])
    monkeypatch.setenv("BRIGADE_HOME", str(tmp_path / "state"))
    monkeypatch.setenv(credentials.ENV_TOKEN_FILE, str(path))
    monkeypatch.setattr(
        fleet_client, "load_fleet_settings", lambda: {"hub_url": base, "node_token": "", "admin_token": ""}
    )
    monkeypatch.setattr(fleet_client, "resolve_node_id", lambda: NODE_A)
    assert cli.main(["fleet", "msg", "list", "--unacked", "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["messages"][0]["id"] == parent["id"]
    monkeypatch.setattr("sys.stdin", io.TextIOWrapper(io.BytesIO(b"Done")))
    assert (
        cli.main(
            [
                "fleet",
                "msg",
                "reply",
                str(parent["id"]),
                "--body-file",
                "-",
                "--client-id",
                "reply-key",
                "--kind",
                "completion",
                "--token-file",
                str(path),
                "--json",
            ]
        )
        == 0
    )
    result = json.loads(capsys.readouterr().out)["message"]
    assert (result["thread"], result["kind"], result["recipient"]) == ("issue:44", "completion", NODE_A)
    assert cli.main(["fleet", "msg", "ack", str(parent["id"]), "--token-file", str(path), "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["message"]["acked_by"] == "agent:fixture"
    assert cli.main(["fleet", "msg", "ack", "99999", "--token-file", str(path), "--json"]) == 1
    error = json.loads(capsys.readouterr().out)
    assert error == {"error": "message not found", "status": 404, "code": None}
