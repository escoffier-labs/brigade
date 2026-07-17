from __future__ import annotations

import json
from pathlib import Path

import pytest

from brigade import cli
from brigade import notifications_cmd
from brigade import center_cmd
from brigade import daily_cmd
from brigade import work_cmd


def _configured_doctor_result(profile: str = "operator") -> notifications_cmd.proc.Result:
    return notifications_cmd.proc.Result(
        code=0,
        stdout=json.dumps(
            {
                "configured": True,
                "selected_profile": profile,
                "selected_channels": ["discord-main"],
                "fail_count": 0,
                "warn_count": 0,
            }
        ),
        stderr="",
    )


def test_notifications_status_reports_missing_agent_notify(monkeypatch, tmp_target, capsys):
    monkeypatch.setattr(notifications_cmd.proc, "which", lambda cmd: None)

    rc = notifications_cmd.status(target=tmp_target, json_output=True)
    out = capsys.readouterr().out
    payload = json.loads(out)

    assert rc == 0
    assert payload["installed"] is False
    assert payload["configured"] is False
    assert payload["probe_exit_code"] == 127
    assert payload["probe_failure_class"] == "not_found"
    assert payload["suggested_next_command"] == "brigade add notifications"
    assert payload["sends_notifications"] is False
    assert payload["writes_hook_config"] is False
    assert payload["stores_secrets"] is False


def test_notifications_status_uses_non_sending_doctor_probe(monkeypatch, tmp_target, capsys):
    seen = {}

    def fake_run(args, timeout=30.0, env=None, cwd=None, stdin=None):
        seen["args"] = args
        seen["timeout"] = timeout
        seen["stdin"] = stdin
        return notifications_cmd.proc.Result(
            code=0,
            stdout=json.dumps(
                {
                    "configured": True,
                    "selected_profile": "operator",
                    "selected_channels": ["telegram-personal"],
                    "fail_count": 0,
                    "warn_count": 0,
                }
            ),
            stderr="",
        )

    monkeypatch.setattr(notifications_cmd.proc, "which", lambda cmd: "/usr/bin/agent-notify")
    monkeypatch.setattr(notifications_cmd.proc, "run", fake_run)

    rc = notifications_cmd.status(target=tmp_target, profile="operator", json_output=True)
    out = capsys.readouterr().out
    payload = json.loads(out)

    assert rc == 0
    assert seen == {
        "args": ["agent-notify", "doctor", "--json", "--skip-network", "--profile", "operator"],
        "timeout": 30.0,
        "stdin": None,
    }
    assert payload["installed"] is True
    assert payload["configured"] is True
    assert payload["status"] == "ok"
    assert payload["selected_profile"] == "operator"
    assert payload["selected_channels"] == ["telegram-personal"]
    assert payload["probe_exit_code"] == 0
    assert payload["probe_failure_class"] is None


def test_notifications_status_discards_failed_doctor_output(monkeypatch, tmp_target, capsys):
    sentinel_url = "https://example.invalid/hook?token=SENTINEL_SECRET"

    def fake_run(args, timeout=30.0, env=None, cwd=None, stdin=None):
        return notifications_cmd.proc.Result(
            code=2,
            stdout=json.dumps({"configured": False, "summary": sentinel_url}),
            stderr=f"provider request failed: {sentinel_url}",
        )

    monkeypatch.setattr(notifications_cmd.proc, "which", lambda cmd: "/usr/bin/agent-notify")
    monkeypatch.setattr(notifications_cmd.proc, "run", fake_run)

    assert notifications_cmd.status(target=tmp_target, profile="operator", json_output=True) == 0
    rendered = capsys.readouterr().out
    payload = json.loads(rendered)

    assert payload["status"] == "warn"
    assert payload["configured"] is False
    assert payload["probe_exit_code"] == 2
    assert payload["probe_failure_class"] == "configuration_error"
    assert "SENTINEL_SECRET" not in rendered
    assert "example.invalid" not in rendered
    assert "stdout_summary" not in rendered
    assert "stderr_summary" not in rendered


def test_notifications_setup_plan_prints_hook_snippets(monkeypatch, tmp_target, capsys):
    monkeypatch.setattr(notifications_cmd.proc, "which", lambda cmd: None)

    rc = notifications_cmd.setup_plan(target=tmp_target, profile="agent-stop")
    out = capsys.readouterr().out

    assert rc == 0
    assert 'notify = ["agent-notify", "--hook", "codex-notify", "--profile", "agent-stop"]' in out
    assert "claude-code-stop --profile agent-stop" in out
    assert "agent-notify has no init or doctor subcommand" not in out
    assert "agent-notify init" not in out


def test_notifications_setup_plan_runs_non_sending_doctor(monkeypatch, tmp_target, capsys):
    seen = {}

    def fake_run(args, timeout=30.0, env=None, cwd=None, stdin=None):
        seen["args"] = args
        return notifications_cmd.proc.Result(code=2, stdout="{}", stderr="ignored")

    monkeypatch.setattr(notifications_cmd.proc, "which", lambda cmd: "/usr/bin/agent-notify")
    monkeypatch.setattr(notifications_cmd.proc, "run", fake_run)

    assert notifications_cmd.setup_plan(target=tmp_target, profile="operator", json_output=True) == 0
    payload = json.loads(capsys.readouterr().out)

    assert seen["args"] == ["agent-notify", "doctor", "--json", "--skip-network", "--profile", "operator"]
    assert payload["doctor_probe"] == {
        "configured": False,
        "probe_exit_code": 2,
        "probe_failure_class": "configuration_error",
    }


def test_notifications_health_is_read_only_when_missing(monkeypatch, tmp_target):
    monkeypatch.setattr(notifications_cmd.proc, "which", lambda cmd: None)

    payload = notifications_cmd.health(tmp_target)

    assert payload["installed"] is False
    assert payload["configured"] is False
    assert payload["status"] == "manual"
    assert payload["issue_count"] == 1
    assert payload["suggested_next_command"] == "brigade add notifications"
    assert payload["sends_notifications"] is False
    assert payload["writes_hook_config"] is False
    assert payload["stores_secrets"] is False


def test_notifications_surface_in_center_work_and_daily(monkeypatch, tmp_target, capsys):
    tmp_target.mkdir()
    monkeypatch.setattr(notifications_cmd.proc, "which", lambda cmd: None)

    center_payload = center_cmd.status_payload(tmp_target)
    assert center_payload["notifications"]["status"] == "manual"

    assert work_cmd.brief(target=tmp_target, json_output=True) == 0
    work_payload = json.loads(capsys.readouterr().out)
    assert work_payload["notifications"]["suggested_next_command"] == "brigade add notifications"

    daily_payload = daily_cmd.status_payload(tmp_target)
    assert daily_payload["notifications"]["issue_count"] == 1

    plan_payload = daily_cmd.plan_payload(tmp_target)
    assert any(item["source_subsystem"] == "notifications" for item in plan_payload["candidate_actions"])


def test_notifications_event_record_writes_local_receipt_without_sending(monkeypatch, tmp_target, capsys):
    tmp_target.mkdir()
    calls = []

    def fake_run(args, timeout=30.0, env=None, cwd=None, stdin=None):
        calls.append(args)
        return _configured_doctor_result()

    monkeypatch.setattr(notifications_cmd.proc, "which", lambda cmd: "/usr/bin/agent-notify")
    monkeypatch.setattr(notifications_cmd.proc, "run", fake_run)

    rc = notifications_cmd.event_record(
        target=tmp_target,
        event_type="handoff-waiting",
        title="Handoff waiting",
        message="Two reviewed handoffs are waiting for ingest.",
        source="handoff",
        json_output=True,
    )
    payload = json.loads(capsys.readouterr().out)

    assert rc == 0
    assert payload["sent"] is False
    assert payload["send_requested"] is False
    assert payload["sends_notifications"] is False
    assert payload["writes_hook_config"] is False
    assert payload["stores_secrets"] is False
    receipt = tmp_target / payload["path"]
    assert receipt.exists()
    assert "agent-notify" in payload["planned_argv"][0]
    assert all(args[1] == "doctor" for args in calls)

    health = notifications_cmd.health(tmp_target)
    assert health["latest_event"]["event_type"] == "handoff-waiting"
    assert all(args[1] == "doctor" for args in calls)


def test_notifications_event_record_can_explicitly_send(monkeypatch, tmp_target, capsys):
    tmp_target.mkdir()
    seen = {}

    def fake_run(args, timeout=30.0, env=None, cwd=None, stdin=None):
        if args[1] == "doctor":
            return _configured_doctor_result()
        seen["args"] = args
        seen["cwd"] = cwd
        seen["stdin"] = stdin
        return notifications_cmd.proc.Result(code=0, stdout="sent", stderr="")

    monkeypatch.setattr(notifications_cmd.proc, "which", lambda cmd: "/usr/bin/agent-notify")
    monkeypatch.setattr(notifications_cmd.proc, "run", fake_run)

    assert (
        cli.main(
            [
                "notifications",
                "event",
                "record",
                "--target",
                str(tmp_target),
                "--type",
                "ci-green",
                "--title",
                "CI green",
                "--message",
                "Brigade CI passed.",
                "--level",
                "success",
                "--profile",
                "operator",
                "--source",
                "ci",
                "--send",
                "--json",
            ]
        )
        == 0
    )
    payload = json.loads(capsys.readouterr().out)
    assert payload["sent"] is True
    assert payload["send_exit_code"] == 0
    assert payload["send_failure_class"] is None
    assert seen["args"] == ["agent-notify", "send", "--profile", "operator"]
    assert seen["stdin"] == (
        b'{"body":"Brigade CI passed.","level":"success","source":"ci","tags":["ci-green"],"title":"CI green"}\n'
    )
    assert seen["cwd"] == tmp_target.resolve()


@pytest.mark.parametrize(
    ("exit_code", "failure_class", "command_exit"),
    [(0, None, 0), (2, "configuration_error", 1), (3, "delivery_error", 1)],
)
def test_notifications_event_record_preserves_send_exit_meanings(
    monkeypatch,
    tmp_target,
    capsys,
    exit_code,
    failure_class,
    command_exit,
):
    tmp_target.mkdir()
    seen = {}

    def fake_run(args, timeout=30.0, env=None, cwd=None, stdin=None):
        if args[1] == "doctor":
            return _configured_doctor_result()
        seen["stdin"] = stdin
        return notifications_cmd.proc.Result(code=exit_code, stdout="ignored", stderr="ignored")

    monkeypatch.setattr(notifications_cmd.proc, "which", lambda cmd: "/usr/bin/agent-notify")
    monkeypatch.setattr(notifications_cmd.proc, "run", fake_run)

    rc = notifications_cmd.event_record(
        target=tmp_target,
        event_type="operator-alert",
        title="Operator alert",
        message="Review the queue.",
        level="warning",
        profile="operator",
        source="brigade",
        send=True,
        json_output=True,
    )
    payload = json.loads(capsys.readouterr().out)

    assert rc == command_exit
    assert payload["send_exit_code"] == exit_code
    assert payload["send_failure_class"] == failure_class
    assert json.loads(seen["stdin"])["level"] == "warn"


def test_notifications_event_record_discards_child_failure_output(monkeypatch, tmp_target, capsys):
    tmp_target.mkdir()
    sentinel_url = "https://example.invalid/hook?token=SENTINEL_SECRET"
    sentinel_token = "TOKEN_VALUE_MUST_NOT_PERSIST"

    def fake_run(args, timeout=30.0, env=None, cwd=None, stdin=None):
        if args[1] == "doctor":
            return _configured_doctor_result()
        return notifications_cmd.proc.Result(
            code=3,
            stdout=f"request={sentinel_url}",
            stderr=f"authorization={sentinel_token}",
        )

    monkeypatch.setattr(notifications_cmd.proc, "which", lambda cmd: "/usr/bin/agent-notify")
    monkeypatch.setattr(notifications_cmd.proc, "run", fake_run)

    assert (
        notifications_cmd.event_record(
            target=tmp_target,
            event_type="ci-failed",
            title="CI failed",
            message="The test job failed.",
            level="error",
            profile="operator",
            source="ci",
            send=True,
            json_output=True,
        )
        == 1
    )
    rendered = capsys.readouterr().out
    payload = json.loads(rendered)
    receipt = json.loads(Path(payload["path"]).read_text())
    persisted = rendered + json.dumps(receipt, sort_keys=True)

    assert payload["send_exit_code"] == 3
    assert payload["send_failure_class"] == "delivery_error"
    assert "SENTINEL_SECRET" not in persisted
    assert sentinel_token not in persisted
    assert "example.invalid" not in persisted
    assert "stdout_summary" not in persisted
    assert "stderr_summary" not in persisted
