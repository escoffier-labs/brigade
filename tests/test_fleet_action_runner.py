"""Fake executables prove argv, durable dispatch fences and transient job behavior."""

import json
import os
import stat
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

from tests.test_fleet_actions import setup_action


requires_posix = pytest.mark.skipif(
    os.name != "posix", reason="Broker execution requires POSIX private files, process groups, and systemd"
)


def fake_executable(tmp_path, payload, *, name="fake-tool"):
    log = tmp_path / (name + ".log")
    executable = tmp_path / name
    executable.write_text(
        "#!" + sys.executable + "\nimport json,sys,os,stat\nfrom pathlib import Path\n"
        "args=sys.argv[1:]\nrecord={'args':args}\n"
        "for flag in ('--prompt-file','--message-file','--body-file'):\n"
        " if flag in args:\n"
        "  p=Path(args[args.index(flag)+1]);record['prompt']=p.read_text(encoding='utf-8');record['mode']=stat.S_IMODE(p.stat().st_mode)\n"
        "with open(" + repr(str(log)) + ",'a') as stream: stream.write(json.dumps(record)+'\\n')\n"
        "print(" + repr(json.dumps(payload)) + ")\n"
    )
    executable.chmod(0o700)
    return executable, log


def manifest(tmp_path, executable, target, *, binding=None):
    path = tmp_path / "manifest.json"
    public = {key: value for key, value in target.items() if key not in {"binding_digest", "node_id"}}
    path.write_text(
        json.dumps(
            {
                "schema": "brigade.fleet_action_manifest.v1",
                "journal_dir": str(tmp_path / "journal"),
                "executables": {
                    "openclaw": str(executable),
                    "t3": str(executable),
                    "brigade": str(executable),
                    "systemd-run": str(executable),
                    "systemctl": str(executable),
                },
                "targets": [{"public": public, "binding": binding or {"agent_id": "fixture-agent"}}],
            }
        )
    )
    path.chmod(0o600)
    return path


def broker_fixture(tmp_path, monkeypatch, *, payload=None):
    from brigade import fleet_action_runner as runner, fleet_client_actions as client, fleet_hub_actions as actions
    from brigade.fleet_action_http import handle_api

    conn, _, grant, request = setup_action(tmp_path)
    exe, log = fake_executable(tmp_path, payload or {"status": "ok", "runId": "fixture-turn"})
    target = json.loads(conn.execute("SELECT document FROM action_targets").fetchone()[0])
    path = manifest(tmp_path, exe, target)
    monkeypatch.setattr(client, "request", lambda body, **kwargs: handle_api(conn, (False, "node-fixture"), body))
    runner.sync_targets(path)
    action = actions.submit(conn, grant, request)
    return conn, path, log, action


@requires_posix
@pytest.mark.parametrize(
    ("key", "value"),
    [
        ("t3", None),
        ("openclaw", False),
        ("brigade", 0),
        ("systemd-run", []),
        ("systemctl", {}),
        ("t3", ""),
        ("openclaw", "bad\x00path"),
        ("brigade", "bad\ud800path"),
    ],
)
def test_invalid_configured_executable_fails_before_transport(tmp_path, monkeypatch, key, value):
    from brigade import fleet_action_auth as auth, fleet_action_runner as runner, fleet_client_actions as client

    executable, log = fake_executable(tmp_path, {"status": "ok"})
    path = manifest(tmp_path, executable, {"target_id": "fixture-target", "family": "t3"})
    data = json.loads(path.read_text())
    data["targets"] = []
    data["executables"][key] = value
    path.write_text(json.dumps(data))
    monkeypatch.setattr(runner.shutil, "which", lambda _: str(executable))
    monkeypatch.setattr(client, "request", lambda *_args, **_kwargs: pytest.fail("invalid manifest reached Hub"))

    with pytest.raises(auth.ActionError, match="invalid-executable"):
        runner.sync_targets(path)

    assert not log.exists()
    assert not (tmp_path / "journal").exists()


@requires_posix
def test_manifest_uses_path_default_only_for_omitted_executable(tmp_path, monkeypatch):
    from brigade import fleet_action_runner as runner

    executable, _ = fake_executable(tmp_path, {"status": "ok"})
    path = manifest(tmp_path, executable, {"target_id": "fixture-target", "family": "t3"})
    data = json.loads(path.read_text())
    data["targets"] = []
    configured = "path-\udcff"
    data["executables"] = {"t3": str(executable), "brigade": configured}
    path.write_text(json.dumps(data))
    monkeypatch.setattr(runner.shutil, "which", lambda name: f"/ambient/{name}")

    loaded = runner.load_manifest(path)

    assert loaded["executables"]["t3"] == str(executable)
    assert loaded["executables"]["brigade"] == configured
    assert loaded["executables"]["openclaw"] == "/ambient/openclaw"
    assert loaded["executables"]["systemctl"] == "/ambient/systemctl"

    monkeypatch.setattr(runner.shutil, "which", lambda _: None)
    loaded = runner.load_manifest(path)
    assert loaded["executables"]["systemctl"] == "systemctl"


@requires_posix
def test_execute_protected_prompt_and_replay_cannot_repeat_turn(tmp_path, monkeypatch):
    from brigade import fleet_action_runner as runner, fleet_hub_actions as actions

    conn, path, log, action = broker_fixture(tmp_path, monkeypatch)
    claimed = actions.claim(conn, "node-fixture")
    result = runner.execute(path, action["action_id"], claimed["generation"])
    assert result["status"] == "finished" and result["verification"] == "pending"
    record = json.loads(log.read_text().splitlines()[-1])
    assert record["mode"] == 0o600
    assert record["args"][:3] == ["agent", "--agent", "fixture-agent"]
    assert "--deliver" not in record["args"] and "--local" not in record["args"]
    assert "雪 <script>bad()</script>" in record["prompt"] and "Attached Hub evidence" in record["prompt"]
    runner.execute(path, action["action_id"], claimed["generation"])
    assert len(log.read_text().splitlines()) == 1
    assert stat.S_IMODE((tmp_path / "journal").stat().st_mode) == 0o700
    conn.close()


@requires_posix
def test_ambiguous_acceptance_is_unknown_and_never_redispatched(tmp_path, monkeypatch):
    from brigade import fleet_action_runner as runner, fleet_hub_actions as actions

    conn, path, log, action = broker_fixture(tmp_path, monkeypatch, payload={"unexpected": "no handle"})
    claimed = actions.claim(conn, "node-fixture")
    assert runner.execute(path, action["action_id"], claimed["generation"])["status"] == "unknown"

    def forbidden(*args, **kwargs):
        pytest.fail("handle-free unknown must not dispatch or recover")

    monkeypatch.setattr(runner.adapters, "dispatch", forbidden)
    monkeypatch.setattr(runner.adapters, "recover", forbidden)
    runner.execute(path, action["action_id"], claimed["generation"])
    assert len(log.read_text().splitlines()) == 1
    conn.close()


@requires_posix
def test_revoke_before_start_never_invokes_destination(tmp_path, monkeypatch):
    from brigade import fleet_action_auth as auth, fleet_action_runner as runner, fleet_hub_actions as actions

    conn, path, log, action = broker_fixture(tmp_path, monkeypatch)
    claimed = actions.claim(conn, "node-fixture")
    auth.revoke_operator(conn, action["grant_id"])
    with pytest.raises(auth.ActionError, match="scope-revoked"):
        runner.execute(path, action["action_id"], claimed["generation"])
    assert not log.exists()
    conn.close()


@requires_posix
def test_tick_schedules_one_unique_transient_job_and_no_inline_turn(tmp_path, monkeypatch):
    from brigade import fleet_action_runner as runner

    conn, path, log, action = broker_fixture(tmp_path, monkeypatch)
    first = runner.tick(path, budget_seconds=15, max_actions=1)
    runner.tick(path, budget_seconds=15, max_actions=1)
    records = [json.loads(line)["args"] for line in log.read_text().splitlines()]
    jobs = [args for args in records if any(arg.startswith("--unit=brigade-action-") for arg in args)]
    assert first["scheduled"] == 1 and len(jobs) == 1
    assert "--user" in jobs[0] and "execute" in jobs[0] and action["action_id"] in jobs[0]
    assert not any(args[:1] == ["agent"] for args in records)
    conn.close()


@requires_posix
def test_executor_unavailable_removes_target_instead_of_shell_fallback(tmp_path, monkeypatch):
    from brigade import fleet_action_runner as runner

    conn, path, _, _ = broker_fixture(tmp_path, monkeypatch)
    value = json.loads(path.read_text())
    value["executables"]["systemd-run"] = str(tmp_path / "absent")
    path.write_text(json.dumps(value))
    assert runner.tick(path)["available"] is False
    assert conn.execute("SELECT count(*) FROM action_targets").fetchone()[0] == 0
    conn.close()


@pytest.mark.parametrize(
    "family,payload,expected",
    [
        (
            "t3",
            {
                "schema": "t3-fleet.result.v1",
                "request_id": "fixture-host-fixture-repo-12345678-1234-1234-1234-123456789abc",
                "state": "running",
            },
            "running",
        ),
        ("grokbot", {"message": {"id": 41, "client_id": "aa_fixture"}}, "accepted"),
        ("dot", {"message": {"id": 42, "client_id": "aa_fixture", "acked_at": "now"}}, "accepted"),
    ],
)
@requires_posix
def test_supported_transport_contracts(tmp_path, monkeypatch, family, payload, expected):
    from brigade import fleet_action_adapters as adapters

    executable, log = fake_executable(tmp_path, payload)
    normal = adapters.invoke

    def invoke(tool, argv, **kw):
        if family != "t3":
            assert tool == Path(sys.executable)
            assert argv[:3] == ["-m", "brigade.fleet_action_adapters", "--inbox-send"]
        return normal(executable, argv, **kw)

    monkeypatch.setattr(adapters, "invoke", invoke)
    prompt = tmp_path / "prompt"
    prompt.write_text("hostile $(touch nowhere) `echo bad` 雪", encoding="utf-8")
    prompt.chmod(0o600)
    target = {"family": family, "principal": "agent:fixture", "capability": "inbox" if family != "t3" else "execution"}
    binding = {
        "host": "fixture-host",
        "repository": "fixture-repo",
        "source_revision": "a" * 40,
        "override_reason": "explicit operator selection",
    }
    action = {"action_id": "aa_fixture", "request": {"target": target, "model_id": "fixture-seat"}}
    receipt = adapters.dispatch(action, binding, executable, prompt)
    assert receipt["status"] == expected
    assert "verification" not in receipt
    record = json.loads(log.read_text())
    args = record["args"]
    assert "$(touch nowhere)" not in " ".join(args)
    if family == "t3":
        assert args[args.index("--seat") + 1] == "fixture-seat"
        assert args[args.index("--source-revision") + 1] == "a" * 40
        assert receipt["destination_ref"] == "fixture-host-fixture-repo-12345678-1234-1234-1234-123456789abc"
    else:
        assert args[args.index("--client-id") + 1] == "aa_fixture"
        assert args[args.index("--to") + 1] == "agent:fixture"
        assert receipt["delivery"] in {"delivered", "acknowledged"}


def test_cli_registers_actions_without_permitting_shell_input():
    from brigade.cli import _build_parser

    parser = _build_parser()
    args = parser.parse_args(["fleet", "actions", "tick", "--manifest", "fixture.json"])
    assert args.func.__module__ == "brigade.cli.fleet_actions"
    assert args.budget_seconds == 15 and args.max_actions == 1
    with pytest.raises(SystemExit):
        parser.parse_args(
            [
                "fleet",
                "actions",
                "execute",
                "aa_fixture",
                "--generation",
                "1",
                "--manifest",
                "fixture.json",
                "--shell",
                "echo bad",
            ]
        )


@pytest.mark.parametrize("family", ["dot", "grokbot"])
@pytest.mark.parametrize("found", [False, True])
@requires_posix
def test_inbox_recovery_without_reference_only_reads_stable_id(tmp_path, family, found):
    from brigade import fleet_action_adapters as adapters

    executable, log = fake_executable(
        tmp_path,
        {"messages": [{"id": 77, "client_id": "aa_fixture", "acked_at": "now"}] if found else []},
    )
    action = {"action_id": "aa_fixture", "request": {"target": {"family": family}}}
    receipt = adapters.recover(action, executable, None)
    # Recovery cannot flush unrelated queued messages or invent pending delivery.
    calls = [json.loads(line)["args"] for line in log.read_text().splitlines()]
    assert all("send" not in args and "flush" not in args for args in calls)
    assert calls == [["fleet", "msg", "sent", "--after", "0", "--json"]]
    assert receipt == (
        {"status": "accepted", "delivery": "acknowledged", "destination_ref": "77"} if found else {"status": "unknown"}
    )


@requires_posix
def test_lost_report_response_replays_saved_receipt_without_another_turn(tmp_path, monkeypatch):
    from brigade import fleet_action_runner as runner, fleet_client_actions as client, fleet_hub_actions as actions

    conn, path, log, action = broker_fixture(tmp_path, monkeypatch)
    claimed = actions.claim(conn, "node-fixture")
    normal = client.request
    lost = False

    def lose_response(body, **kwargs):
        nonlocal lost
        if body["op"] == "report" and not lost:
            lost = True
            raise OSError("fixture lost reply")
        return normal(body, **kwargs)

    monkeypatch.setattr(client, "request", lose_response)
    with pytest.raises(OSError, match="lost reply"):
        runner.execute(path, action["action_id"], claimed["generation"])
    assert runner.execute(path, action["action_id"], claimed["generation"])["status"] == "finished"
    assert len(log.read_text().splitlines()) == 1
    assert conn.execute("SELECT count(*) FROM action_events WHERE event_id LIKE 'result_%'").fetchone()[0] == 1
    conn.close()


@pytest.mark.parametrize("family", ["dot", "grokbot"])
@requires_posix
def test_initial_inbox_send_leaves_unrelated_credential_spool_untouched(tmp_path, monkeypatch, capsys, family):
    from brigade import fleet_action_adapters as adapters, fleet_client, fleet_client_messages as messages
    from brigade import fleet_client_message_credentials as credentials

    monkeypatch.setattr(
        credentials, "message_settings", lambda *a, **kw: ("https://hub.example.test", "x" * 32, "fixture")
    )
    monkeypatch.setattr(fleet_client, "spool_path", lambda node: tmp_path / "spool" / "node.jsonl")
    authority = credentials.MessageAuthority.capture()
    spool = messages._spool_path(authority.node_id, authority.token, hub_url=authority.hub)
    spool.parent.mkdir(mode=0o700)
    old = {
        "action": "send",
        "to": "agent:other",
        "subject": "Unrelated old subject",
        "body": "old body",
        "ref": "old",
        "thread": "old",
        "priority": "normal",
        "client_id": "old-client",
        "enqueued_at": datetime.now(timezone.utc).isoformat(),
    }
    original = (json.dumps(old) + "\n").encode()
    spool.write_bytes(original)
    spool.chmod(0o600)
    seen = []

    def request(hub, token, body, **kw):
        seen.append(body.copy())
        return 200, {"message": {"id": 42, "client_id": body["client_id"]}}

    monkeypatch.setattr(messages, "_request", request)

    def invoke(executable, argv, **kw):
        if argv[:3] == ["fleet", "msg", "send"]:
            # Exercise the actual old route, including its credential-wide drain.
            return messages.send(
                recipient=argv[argv.index("--to") + 1],
                subject=argv[argv.index("--subject") + 1],
                body=prompt.read_text(encoding="utf-8"),
                reference="aa_fixture",
                priority="normal",
                thread="aa_fixture",
                client_id="aa_fixture",
            )
        assert executable == Path(sys.executable)
        assert argv[:3] == ["-m", "brigade.fleet_action_adapters", "--inbox-send"]
        assert kw == {"timeout": 15}
        assert adapters._inbox_main(argv[2:]) == 0
        return json.loads(capsys.readouterr().out)

    monkeypatch.setattr(adapters, "invoke", invoke)
    prompt = tmp_path / "prompt"
    prompt.write_text("hostile $(touch nowhere) `echo bad` 雪", encoding="utf-8")
    action = {"action_id": "aa_fixture", "request": {"target": {"family": family, "principal": "agent:fixture"}}}
    receipt = adapters.dispatch(action, {}, tmp_path / "unused-brigade", prompt)
    assert receipt == {"status": "accepted", "delivery": "delivered", "destination_ref": "42"}
    assert seen == [
        {
            "action": "send",
            "to": "agent:fixture",
            "subject": "Repair alert aa_fixture",
            "body": "hostile $(touch nowhere) `echo bad` 雪",
            "ref": "aa_fixture",
            "thread": "aa_fixture",
            "client_id": "aa_fixture",
            "priority": "normal",
        }
    ]
    assert spool.read_bytes() == original


@pytest.fixture(params=["authority", "legacy"])
def inbox_online_api(request, monkeypatch):
    from brigade import fleet_client_message_credentials as credentials, fleet_client_messages as messages

    settings = ("https://hub.example.test", "x" * 32, "fixture")
    monkeypatch.setattr(credentials, "message_settings", lambda *a, **kw: settings)
    if request.param == "legacy":
        # Exact older co-versioned private API: settings selection is inside _online.
        def online(body, *, token_file=None, timeout=None):
            assert token_file is None
            hub, token, _ = credentials.message_settings(token_file)
            status, payload = messages._request(hub, token, body, timeout=timeout)
            if status != 200:
                raise RuntimeError("private upstream error")
            return payload

        monkeypatch.setattr(messages, "_online", online)
    return request.param


def inbox_args(prompt):
    return ["--inbox-send", "--to", "agent:fixture", "--client-id", "aa_fixture", "--body-file", str(prompt)]


@pytest.mark.parametrize("failure", ["outage", "wrong-client", "queued"])
@requires_posix
def test_inbox_helper_unknown_never_queues_or_retries(tmp_path, monkeypatch, capsys, inbox_online_api, failure):
    from brigade import fleet_action_adapters as adapters, fleet_client, fleet_client_messages as messages

    attempts = []

    def request(hub, token, body, **kw):
        attempts.append(body)
        if failure == "outage":
            raise OSError("private credential and prompt details")
        return 200, (
            {"queued": True, "client_id": "aa_fixture"}
            if failure == "queued"
            else {"message": {"id": 42, "client_id": "wrong-client"}}
        )

    def forbidden(*args, **kwargs):
        pytest.fail("online helper must not access any spool")

    monkeypatch.setattr(messages, "_request", request)
    for name in ("send", "_send_ordered", "_drain_locked", "_queue_locked", "flush", "_spool_path"):
        monkeypatch.setattr(messages, name, forbidden)
    monkeypatch.setattr(fleet_client, "_read_spool", forbidden)
    prompt = tmp_path / "body"
    prompt.write_text("data only")
    assert adapters._inbox_main(inbox_args(prompt)) == 1
    assert json.loads(capsys.readouterr().out) == {"error": "transport-acceptance-unknown"}
    assert len(attempts) == 1
    assert sorted(path.name for path in tmp_path.iterdir()) == ["body"]


@pytest.mark.parametrize(
    "malformed",
    [
        "flags",
        "client",
        "recipient",
        *[
            pytest.param(case, marks=requires_posix)
            for case in ("oversize", "utf8", "empty", "missing", "fifo", "symlink")
        ],
    ],
)
def test_inbox_helper_rejects_malformed_input_before_transport(tmp_path, monkeypatch, capsys, malformed):
    from brigade import (
        fleet_action_adapters as adapters,
        fleet_client_messages as messages,
        fleet_hub_messages as limits,
    )

    prompt = tmp_path / "body"
    prompt.write_text("data only")
    args = inbox_args(prompt)
    if malformed == "flags":
        args += ["--arbitrary", "private-data"]
    elif malformed == "client":
        args[4] = "invalid client id"
    elif malformed == "recipient":
        args[2] = " "
    elif malformed == "oversize":
        prompt.write_bytes(b"x" * (limits.BODY_LIMIT + 1))
    elif malformed == "utf8":
        prompt.write_bytes(b"\xff")
    elif malformed == "empty":
        prompt.write_text(" ")
    elif malformed == "missing":
        prompt.unlink()
    elif malformed == "fifo":
        prompt.unlink()
        os.mkfifo(prompt)
    elif malformed == "symlink":
        prompt.rename(tmp_path / "other")
        prompt.symlink_to(tmp_path / "other")

    def forbidden(*args, **kwargs):
        pytest.fail("malformed data must fail before credential capture or transport")

    monkeypatch.setattr(messages.MessageAuthority, "capture", forbidden)
    monkeypatch.setattr(messages, "_online", forbidden)
    assert adapters._inbox_main(args) == 1
    assert json.loads(capsys.readouterr().out) == {"error": "transport-acceptance-unknown"}


@pytest.mark.parametrize(
    "hub,allowed",
    [("http://remote.example.test", False), ("https://hub.example.test", True), ("http://127.0.0.1", True)],
)
@requires_posix
def test_inbox_helper_retains_transport_endpoint_guard(tmp_path, monkeypatch, capsys, inbox_online_api, hub, allowed):
    import io
    from brigade import fleet_action_adapters as adapters, fleet_client, fleet_client_message_credentials as credentials

    monkeypatch.setattr(credentials, "message_settings", lambda *a, **kw: (hub, "x" * 32, "fixture"))
    opened = []

    def hub_open(request, **kw):
        opened.append(request.full_url)
        response = io.BytesIO(b'{"message":{"id":42,"client_id":"aa_fixture"}}')
        response.status = 200
        return response

    monkeypatch.setattr(fleet_client, "_hub_open", hub_open)
    prompt = tmp_path / "body"
    prompt.write_text("data only")
    assert adapters._inbox_main(inbox_args(prompt)) == (0 if allowed else 1)
    output = json.loads(capsys.readouterr().out)
    assert opened == ([hub + "/messages"] if allowed else [])
    assert output == (
        {"message": {"id": 42, "client_id": "aa_fixture"}} if allowed else {"error": "transport-acceptance-unknown"}
    )


@requires_posix
def test_inbox_helper_validates_captured_authority_before_transport(tmp_path, monkeypatch, capsys):
    from brigade import fleet_action_adapters as adapters, fleet_client_message_credentials as credentials
    from brigade import fleet_client_messages as messages

    calls = []

    def validate(self):
        calls.append("validate")
        raise credentials.MessageAuthorityError("private rotated credential")

    monkeypatch.setattr(
        credentials, "message_settings", lambda *a, **kw: ("https://hub.example.test", "x" * 32, "fixture")
    )
    monkeypatch.setattr(credentials.MessageAuthority, "validate", validate)
    monkeypatch.setattr(messages, "_request", lambda *a, **kw: pytest.fail("invalid authority reached transport"))
    prompt = tmp_path / "body"
    prompt.write_text("data only")
    assert adapters._inbox_main(inbox_args(prompt)) == 1
    assert calls == ["validate"]
    assert json.loads(capsys.readouterr().out) == {"error": "transport-acceptance-unknown"}


@requires_posix
def test_inbox_helper_unknown_private_api_fails_closed(tmp_path, monkeypatch, capsys):
    from brigade import fleet_action_adapters as adapters, fleet_client_messages as messages

    def unsupported(body, **kwargs):
        pytest.fail("unknown private contract must never be called")

    monkeypatch.setattr(messages, "_online", unsupported)
    prompt = tmp_path / "body"
    prompt.write_text("data only")
    assert adapters._inbox_main(inbox_args(prompt)) == 1
    assert json.loads(capsys.readouterr().out) == {"error": "transport-acceptance-unknown"}


def test_native_transport_refuses_non_posix_before_process_launch(tmp_path, monkeypatch):
    from types import SimpleNamespace
    from brigade import fleet_action_adapters as adapters
    from brigade.fleet_action_auth import ActionError

    monkeypatch.setattr(adapters, "os", SimpleNamespace(name="nt"))
    monkeypatch.setattr(adapters.subprocess, "Popen", lambda *a, **kw: pytest.fail("unsupported transport launched"))
    with pytest.raises(ActionError, match="transport-platform-unsupported"):
        adapters.invoke(tmp_path / "unused", [])


def test_broker_journal_refuses_non_posix_before_file_access(tmp_path, monkeypatch):
    from types import SimpleNamespace
    from brigade import fleet_action_runner as runner
    from brigade.fleet_action_auth import ActionError

    monkeypatch.setattr(runner, "os", SimpleNamespace(name="nt"))
    monkeypatch.setattr(
        runner.fleet_client, "_ensure_private_dir", lambda *a: pytest.fail("unsupported journal opened")
    )
    with pytest.raises(ActionError, match="transient-executor-unavailable"):
        with runner._journal_lock(tmp_path / "journal", "fixture"):
            pytest.fail("unsupported journal acquired")
    assert not (tmp_path / "journal").exists()


def test_inbox_helper_without_secure_file_flags_refuses_before_transport(tmp_path, monkeypatch, capsys):
    from types import SimpleNamespace
    from brigade import fleet_action_adapters as adapters

    prompt = tmp_path / "body"
    prompt.write_text("data only", encoding="utf-8")
    monkeypatch.setattr(adapters, "os", SimpleNamespace(O_RDONLY=0))
    monkeypatch.setattr(adapters, "_inbox_online", lambda *a: pytest.fail("unsupported file access reached transport"))
    assert adapters._inbox_main(inbox_args(prompt)) == 1
    assert json.loads(capsys.readouterr().out) == {"error": "transport-acceptance-unknown"}
