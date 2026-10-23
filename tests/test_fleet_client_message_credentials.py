"""Protected external credentials and the bridge-facing message CLI contract."""

import io
import json
import os
import sys
import urllib.error
import urllib.request

import pytest

from brigade import cli, fleet_client, fleet_client_messages as client
from brigade import fleet_client_message_credentials as credentials
from tests.test_fleet_messages import NODE_A, NODE_B
from tests.test_fleet_message_grants import enroll

pytest_plugins = ["tests._fleet_message_fixtures"]

POSIX_MODE = pytest.mark.skipif(sys.platform == "win32", reason="Credential POSIX mode checks do not apply on Windows")
POSIX_OWNER = pytest.mark.skipif(sys.platform == "win32", reason="Windows has no POSIX credential owner UID")
POSIX_FIFO = pytest.mark.skipif(sys.platform == "win32", reason="Windows has no POSIX mkfifo credential fixture")


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
    assert client._spool_path(
        NODE_A, flag[1], hub_url=fleet_client.load_fleet_settings()["hub_url"]
    ) != client._spool_path(NODE_A, env[1], hub_url=fleet_client.load_fleet_settings()["hub_url"])


@pytest.mark.parametrize(
    "invalid",
    [
        "symlink",
        pytest.param("mode", marks=POSIX_MODE),
        pytest.param("owner", marks=POSIX_OWNER),
        "oversize",
        "charset",
        "newline",
        "directory",
        pytest.param("fifo", marks=POSIX_FIFO),
        "admin",
    ],
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


def test_external_principal_denial_closes_response(hub):
    base, _, _, _ = hub
    principal = enroll(base)
    request = urllib.request.Request(
        base + "/messages",
        data=json.dumps({"action": "send", "to": NODE_B, "subject": "Task", "body": "Work"}).encode(),
        headers={"Authorization": f"Bearer {principal['token']}", "Content-Type": "application/json"},
    )
    with pytest.raises(urllib.error.HTTPError) as failure:
        urllib.request.urlopen(request, timeout=5)
    with failure.value as response:
        assert response.code == 403
        assert response.headers.get_all("Connection") == ["close"]
        assert json.loads(response.read())["code"] == "message_recipient_not_permitted"


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
    empty = client._spool_path(NODE_A, "empty-fixture", hub_url=fleet_client.load_fleet_settings()["hub_url"])
    empty.touch(mode=0o600)
    caplog.clear()
    client._warn_inactive_spools(
        NODE_A, client._spool_path(NODE_A, "active-fixture", hub_url=fleet_client.load_fleet_settings()["hub_url"])
    )
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


def test_pending_enrollment_preserves_grants(configured, monkeypatch, caplog):
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
    assert "pending enrollment retains its original send grants" in caplog.text


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
    path = client._spool_path(NODE_A, "fixture-node", hub_url=fleet_client.load_fleet_settings()["hub_url"])
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
    principal = enroll(base, may_send_to=[NODE_A])
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


@pytest.mark.parametrize(
    "invalid", [None, pytest.param("mode", marks=POSIX_MODE), "newline", "symlink", "oversize", "charset", "directory"]
)
def test_token_file_without_geteuid_keeps_other_checks(configured, tmp_path, monkeypatch, invalid):
    path = token_file(tmp_path)
    if invalid == "mode":
        path.chmod(0o644)
    elif invalid == "newline":
        path.write_text(path.read_text() + "\n")
    elif invalid == "symlink":
        link = tmp_path / "link"
        link.symlink_to(path)
        path = link
    elif invalid == "oversize":
        path.write_text("x" * 4097)
    elif invalid == "charset":
        path.write_text("!" * 40)
    elif invalid == "directory":
        path.unlink()
        path.mkdir(mode=0o700)
    monkeypatch.delattr(os, "geteuid", raising=False)
    if invalid is None:
        assert credentials.read_message_token_file(str(path)) == path.read_text()
    else:
        with pytest.raises(fleet_client.FleetClientError):
            credentials.read_message_token_file(str(path))


@pytest.mark.parametrize("action", ["send", "reply"])
def test_queued_client_id_retry_is_unique_and_conflicts_locally(configured, monkeypatch, action):
    calls = []

    def offline(_hub, _token, body, **_kwargs):
        calls.append(body)
        raise fleet_client.FleetClientError("offline")

    monkeypatch.setattr(client, "_request", offline)

    def submit(body="Work"):
        fields = {"subject": "Task", "body": body, "reference": None, "priority": "normal", "client_id": "same"}
        return client.send(recipient=NODE_B, **fields) if action == "send" else client.reply(42, **fields)

    first = submit()
    assert submit() == first
    path = client._spool_path(NODE_A, "fixture-node", hub_url=fleet_client.load_fleet_settings()["hub_url"])
    original = fleet_client._read_spool(path)
    assert len(original) == 1
    with pytest.raises(client.FleetMessageHubError) as error:
        submit("Different")
    assert error.value.status is None
    assert error.value.code == "spool_conflict"
    assert fleet_client._read_spool(path) == original
    assert len(calls) == 1


def test_spooled_client_id_cannot_discard_other_recipient(configured, monkeypatch):
    calls = []

    def deliver(_hub, _token, body, **_kwargs):
        calls.append(body["to"])
        if body["to"] == NODE_A:
            return 400, {"code": "message_recipient_inactive"}
        return 201, {"message": {"id": 42, "client_id": body["client_id"]}}

    monkeypatch.setattr(client, "_request", deliver)
    fields = {"subject": "Task", "body": "Work", "reference": None, "priority": "normal", "client_id": "same"}
    assert client.send(recipient=NODE_A, **fields)["queued"] is True
    with pytest.raises(client.FleetMessageHubError, match="client_id"):
        client.send(recipient=NODE_B, **fields)
    path = client._spool_path(NODE_A, "fixture-node", hub_url=fleet_client.load_fleet_settings()["hub_url"])
    assert [row["to"] for row in fleet_client._read_spool(path)] == [NODE_A]
    assert NODE_B not in calls
    assert "message" in client.send(recipient=NODE_B, **{**fields, "client_id": "other"})
    assert [row["to"] for row in fleet_client._read_spool(path)] == [NODE_A]


def test_no_grants_enrollment_uses_v30_wire_body(configured, monkeypatch):
    monkeypatch.setattr(client, "_principal_enrollment_generation", lambda *_args: None)
    calls = []

    def legacy_hub(_hub, _token, body):
        calls.append(body)
        if "may_send_to" in body:
            return 400, {"error": "unknown field"}
        return 201, {"principal": {**body, "principal_id": "agent:fixture", "enrollment_id": body["request_id"]}}

    monkeypatch.setattr(client, "_principal_admin_request", legacy_hub)
    assert client.manage_principal("add", name="fixture")["principal"]["principal_id"] == "agent:fixture"
    assert "may_send_to" not in calls[0]


@pytest.mark.parametrize(
    "failure,code",
    [
        ("token", "token_file_invalid"),
        ("client-id", "client_id_invalid"),
        ("admin", "admin_token_refused"),
        ("body", "invalid_argument"),
        ("stdin", "invalid_argument"),
        ("spool", "spool_conflict"),
    ],
)
def test_cli_local_json_codes(configured, tmp_path, monkeypatch, capsys, failure, code):
    monkeypatch.setattr(
        client, "_request", lambda *_args, **_kwargs: (_ for _ in ()).throw(fleet_client.FleetClientError("offline"))
    )
    fields = {
        "recipient": NODE_B,
        "subject": "Task",
        "body": "Original",
        "reference": None,
        "priority": "normal",
        "client_id": "stable",
    }
    if failure == "spool":
        assert client.send(**fields)["queued"] is True
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
        "--json",
    ]
    raw = b"Different"
    if failure == "token":
        argv += ["--token-file", str(tmp_path / "missing")]
    elif failure == "client-id":
        argv += ["--client-id", "bad/id"]
    elif failure == "admin":
        monkeypatch.setattr(
            fleet_client,
            "load_fleet_settings",
            lambda: {"hub_url": "https://hub.invalid", "node_token": "fixture-admin", "admin_token": "fixture-admin"},
        )
    elif failure == "body":
        raw = b""
    elif failure == "stdin":
        raw = b"x" * 65537
    monkeypatch.setattr("sys.stdin", io.TextIOWrapper(io.BytesIO(raw)))
    assert cli.main(argv) == 1
    result = json.loads(capsys.readouterr().out)
    assert result["status"] is None
    assert result["code"] == code


def test_cli_transport_failure_remains_distinct(configured, monkeypatch, capsys):
    def unavailable(*_args, **_kwargs):
        raise OSError("offline")

    monkeypatch.setattr(fleet_client, "_hub_open", unavailable)
    assert cli.main(["fleet", "msg", "list", "--json"]) == 1
    result = json.loads(capsys.readouterr().out)
    assert result["status"] is None
    assert result["code"] == "hub_unavailable"


def test_enrollment_grants_conflict_clears_stale_pending(hub, tmp_path, monkeypatch):
    from tests.test_fleet_messages import ADMIN
    from tests.test_fleet_message_grants import set_grants

    base, _, _, _ = hub
    monkeypatch.setenv("BRIGADE_HOME", str(tmp_path / "state"))
    monkeypatch.setattr(
        fleet_client, "load_fleet_settings", lambda: {"hub_url": base, "node_token": "", "admin_token": ADMIN}
    )
    ids = []
    client.manage_principal(
        "add", name="fixture", may_send_to=[NODE_A], _defer_finalize=True, _deferred_request_ids=ids
    )
    assert set_grants(base, [NODE_B])[0] == 200
    with pytest.raises(client.FleetMessageHubError) as error:
        client.manage_principal("add", name="fixture", may_send_to=[NODE_A])
    assert error.value.status == 409
    assert error.value.code == "principal_enrollment_grants_changed"
    path = client._principal_pending_path("fixture", ADMIN, hub_url=fleet_client.load_fleet_settings()["hub_url"])
    assert fleet_client._read_spool(path) == []
    # The stale replay is gone. The current principal remains inspectable and can be re-enrolled explicitly.
    assert client.show_principal("fixture")["principal"]["may_send_to"] == [NODE_B]
    with pytest.raises(client.FleetMessageHubError, match="already exists") as retry:
        client.manage_principal("add", name="fixture")
    assert retry.value.status == 400
    client.manage_principal("revoke", name="fixture")
    fresh = client.manage_principal("add", name="fixture", may_send_to=[NODE_B])
    assert fresh["principal"]["enrollment_id"] != ids[0]


def test_disappearing_legacy_spool_does_not_abort_send(configured, monkeypatch):
    from pathlib import Path

    legacy = fleet_client.spool_path(NODE_A).parent / f"{NODE_A}.messages.jsonl"
    legacy.parent.mkdir(parents=True, exist_ok=True)
    legacy.touch(mode=0o600)
    original = Path.stat
    checks = 0

    def vanished(path, *args, **kwargs):
        nonlocal checks
        if path == legacy:
            checks += 1
            if checks == 3:
                raise FileNotFoundError("legacy spool disappeared")
        return original(path, *args, **kwargs)

    monkeypatch.setattr(Path, "stat", vanished)
    monkeypatch.setattr(
        client,
        "_request",
        lambda _hub, _token, body, **_kwargs: (201, {"message": {"id": 42, "client_id": body["client_id"]}}),
    )
    assert (
        client.send(recipient=NODE_B, subject="Task", body="Work", reference=None, priority="normal")["message"]["id"]
        == 42
    )


def test_confirmed_spool_removal_preserves_other_entry_with_same_client_id(configured):
    path = client._spool_path(NODE_A, "fixture-node", hub_url=fleet_client.load_fleet_settings()["hub_url"])
    confirmed = {"action": "send", "client_id": "legacy-duplicate", "to": NODE_A, "body": "First"}
    other = {**confirmed, "to": NODE_B, "body": "Second"}
    with fleet_client._spool_lock(path) as fd:
        fleet_client._write_spool_atomic(path, [confirmed, other], dir_fd=fd)
        client._remove_queued_locked(path, confirmed, dir_fd=fd)
    assert fleet_client._read_spool(path) == [other]


@pytest.mark.parametrize("invalid", [None, "symlink", "oversize", "charset", "directory"])
def test_windows_skips_posix_mode_and_owner_bits_only(configured, tmp_path, monkeypatch, invalid):
    path = token_file(tmp_path)
    # Windows synthesizes 0666 for writable files and chmod cannot clear group/other bits.
    path.chmod(0o666)
    if invalid == "symlink":
        link = tmp_path / "link"
        link.symlink_to(path)
        path = link
    elif invalid == "oversize":
        path.write_text("x" * 4097)
    elif invalid == "charset":
        path.write_text("!" * 40)
    elif invalid == "directory":
        path.unlink()
        path.mkdir(mode=0o700)
    monkeypatch.setattr(os, "geteuid", lambda: -1, raising=False)
    monkeypatch.setattr(sys, "platform", "win32")
    if invalid is None:
        assert credentials.read_message_token_file(str(path)) == path.read_text()
    else:
        with pytest.raises(fleet_client.FleetClientError):
            credentials.read_message_token_file(str(path))


def test_captured_authority_is_frozen_secret_safe_and_validation_never_adopts(configured, tmp_path):
    from dataclasses import FrozenInstanceError

    sentinel = "SENTINEL_SECRET_NEVER_ECHO_" * 2
    path = token_file(tmp_path, value=sentinel)
    authority = credentials.MessageAuthority.capture(str(path))
    assert sentinel not in repr(authority) and str(path) not in repr(authority)
    with pytest.raises(FrozenInstanceError):
        authority.token = "replacement"
    path.write_text("rotated_" * 8)
    with pytest.raises(credentials.MessageAuthorityError) as error:
        authority.validate()
    assert error.value.code == "token_scope_changed"
    assert sentinel not in str(error.value) + repr(error.value)
    assert authority.token == sentinel  # Comparison never adopts current credentials.


@pytest.mark.parametrize("operation", ["send", "flush", "list"])
def test_upstream_errors_never_expose_authority_in_errors_logs_or_spool(
    configured, tmp_path, monkeypatch, caplog, operation
):
    sentinel = "SENTINEL_SECRET_NEVER_ECHO_" * 2
    path = token_file(tmp_path, value=sentinel)
    params = dict(
        recipient=NODE_B, subject="subject", body="body", reference=None, priority="normal", client_id="stable"
    )
    if operation == "flush":
        monkeypatch.setattr(client, "_request", lambda *a, **k: (503, {}))
        assert client.send(**params, token_file=str(path))["queued"]
    monkeypatch.setattr(client, "_request", lambda *a, **k: (400, {"error": sentinel, "code": sentinel}))
    if operation == "flush":
        assert client.flush_credential(str(path)) == 0
        visible = caplog.text
    else:
        with pytest.raises(credentials.FleetMessageHubError) as error:
            if operation == "send":
                client.send(**params, token_file=str(path))
            else:
                client.list_messages(token_file=str(path))
        visible = caplog.text + str(error.value) + repr(error.value)
    persisted = "".join(p.read_text() for p in (tmp_path / "state").rglob("*.jsonl"))
    assert sentinel not in visible + persisted


def test_deferred_cli_finalize_never_adopts_changed_endpoint(configured, tmp_path, monkeypatch, capsys):
    settings = dict(hub_url="https://hub.invalid", node_token="fixture-node", admin_token="fixture-admin")
    monkeypatch.setattr(fleet_client, "load_fleet_settings", lambda: dict(settings))
    requests = []

    def admin_request(hub, token, body):
        requests.append(hub)
        if body["action"] == "get":
            return 404, {}
        return 200, {
            "principal": dict(
                name="fixture", principal_id="agent:fixture", enrollment_id=body["request_id"], token=body["token"]
            )
        }

    original_finalize = client.finalize_principal_add

    def finalize(*args, **kwargs):
        settings["hub_url"] = "https://other.invalid"
        return original_finalize(*args, **kwargs)

    monkeypatch.setattr(client, "_principal_admin_request", admin_request)
    monkeypatch.setattr(client, "finalize_principal_add", finalize)
    # Successful output delivery cannot finalize against a newly selected endpoint.
    assert cli.main(["fleet", "msg", "principals", "add", "fixture", "--json"]) == 1
    capsys.readouterr()
    assert requests == ["https://hub.invalid"] * 2
    pending = list((tmp_path / "state").rglob("principal-*.jsonl"))
    assert len(pending) == 1 and fleet_client._read_spool(pending[0])


def test_exception_text_and_unknown_code_from_transport_are_closed(configured, monkeypatch, caplog):
    sentinel = "SENTINEL_SECRET_NEVER_ECHO_" * 2

    def request(*args, **kwargs):
        raise credentials.FleetMessageHubError(sentinel, status=403, code=sentinel)

    monkeypatch.setattr(client, "_request", request)
    with pytest.raises(credentials.FleetMessageHubError) as error:
        client.list_messages()
    assert sentinel not in str(error.value) + repr(error.value) + caplog.text
    assert error.value.code is None


def test_node_flush_keeps_node_authority_with_message_override(configured, tmp_path, monkeypatch):
    params = dict(
        recipient=NODE_B, subject="subject", body="body", reference=None, priority="normal", client_id="node-retry"
    )
    monkeypatch.setattr(client, "_request", lambda *a, **k: (503, {}))
    assert client.send(**params)["queued"]
    principal = token_file(tmp_path)
    monkeypatch.setenv(credentials.ENV_TOKEN_FILE, str(principal))
    calls = []

    def request(hub, token, body, **kwargs):
        calls.append(token == "fixture-node")
        return 200, {"message": {"client_id": body["client_id"]}}

    monkeypatch.setattr(client, "_request", request)
    assert client.flush(NODE_A) == 1
    assert calls == [True]


@pytest.mark.parametrize("phase", ["replay", "reply-lookup"])
def test_rotation_between_replay_requests_stops_and_retains_original_spool(configured, tmp_path, monkeypatch, phase):
    from datetime import datetime, timezone

    path = token_file(tmp_path)
    authority = credentials.MessageAuthority.capture(str(path))
    spool = client._spool_path(NODE_A, authority.token, hub_url=authority.hub)
    stamp = datetime.now(timezone.utc).isoformat()
    queued = [
        dict(action="send", to=NODE_B, client_id="first", enqueued_at=stamp),
        dict(action="reply", id=9, client_id="reply", enqueued_at=stamp),
    ]
    with fleet_client._spool_lock(spool) as descriptor:
        fleet_client._write_spool_atomic(spool, queued, dir_fd=descriptor)
    original = spool.read_bytes()
    calls = []

    def request(hub, token, body, **kwargs):
        calls.append(body["action"])
        if phase == "reply-lookup" and body["action"] == "send":
            return 429, {"code": "message_recipient_quota", "recipient": NODE_B}
        path.write_text("y" * 64)
        if body["action"] == "list":
            return 200, {"messages": [{"id": 9, "sender": NODE_A}], "next_cursor": 9}
        return 200, {"message": {"client_id": body["client_id"]}}

    monkeypatch.setattr(client, "_request", request)
    with pytest.raises(credentials.MessageAuthorityError) as error:
        client.flush_credential(str(path))
    assert error.value.code == "token_scope_changed"
    assert calls == (["send"] if phase == "replay" else ["send", "list"])
    assert spool.read_bytes() == original


def test_unconfigured_message_cli_keeps_local_error_contract(configured, monkeypatch, capsys):
    monkeypatch.setattr(fleet_client, "load_fleet_settings", lambda: dict(hub_url="", node_token="", admin_token=""))
    assert cli.main(["fleet", "msg", "list", "--json"]) == 1
    result = json.loads(capsys.readouterr().out)
    assert result["status"] is None and result["code"] == "invalid_argument"
