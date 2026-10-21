"""Fake executables prove argv, durable dispatch fences and transient job behavior."""

import json
import stat
import sys

import pytest

from tests.test_fleet_actions import setup_action


def fake_executable(tmp_path, payload, *, name="fake-tool"):
    log = tmp_path / (name + ".log")
    executable = tmp_path / name
    executable.write_text(
        "#!" + sys.executable + "\nimport json,sys,os,stat\nfrom pathlib import Path\n"
        "args=sys.argv[1:]\nrecord={'args':args}\n"
        "for flag in ('--prompt-file','--message-file','--body-file'):\n"
        " if flag in args:\n"
        "  p=Path(args[args.index(flag)+1]);record['prompt']=p.read_text();record['mode']=stat.S_IMODE(p.stat().st_mode)\n"
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


def test_revoke_before_start_never_invokes_destination(tmp_path, monkeypatch):
    from brigade import fleet_action_auth as auth, fleet_action_runner as runner, fleet_hub_actions as actions

    conn, path, log, action = broker_fixture(tmp_path, monkeypatch)
    claimed = actions.claim(conn, "node-fixture")
    auth.revoke_operator(conn, action["grant_id"])
    with pytest.raises(auth.ActionError, match="scope-revoked"):
        runner.execute(path, action["action_id"], claimed["generation"])
    assert not log.exists()
    conn.close()


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
        ("grokbot", {"queued": True, "client_id": "aa_fixture"}, "accepted"),
        ("dot", {"message": {"id": 42, "client_id": "aa_fixture", "acked_at": "now"}}, "accepted"),
    ],
)
def test_supported_transport_contracts(tmp_path, family, payload, expected):
    from brigade import fleet_action_adapters as adapters

    executable, log = fake_executable(tmp_path, payload)
    prompt = tmp_path / "prompt"
    prompt.write_text("hostile $(touch nowhere) `echo bad` 雪")
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
        assert receipt["delivery"] in {"pending", "acknowledged"}


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
