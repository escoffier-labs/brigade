from __future__ import annotations

import json

from brigade import cli
from brigade import notifications_cmd
from brigade import center_cmd
from brigade import daily_cmd
from brigade import work_cmd


def test_notifications_status_reports_missing_agent_notify(monkeypatch, tmp_target, capsys):
    monkeypatch.setattr(notifications_cmd.proc, "which", lambda cmd: None)

    rc = notifications_cmd.status(target=tmp_target, json_output=True)
    out = capsys.readouterr().out
    payload = json.loads(out)

    assert rc == 0
    assert payload["installed"] is False
    assert payload["configured"] is False
    assert payload["suggested_next_command"] == "brigade add notifications"
    assert payload["sends_notifications"] is False
    assert payload["writes_hook_config"] is False
    assert payload["stores_secrets"] is False


def test_notifications_status_reads_config_and_env(monkeypatch, tmp_target, capsys):
    config_path = tmp_target / ".config" / "agent-notify" / "config.toml"
    config_path.parent.mkdir(parents=True)
    config_path.write_text(
        "\n".join(
            [
                "[channels.telegram-personal]",
                'type = "telegram"',
                'bot_token_env = "TEST_TELEGRAM_BOT_TOKEN"',
                'chat_id_env = "TEST_TELEGRAM_CHAT_ID"',
                "",
                "[profiles.operator]",
                'channels = ["telegram-personal"]',
                "default = true",
                "",
            ]
        )
    )
    monkeypatch.setattr(notifications_cmd, "CONFIG_PATH", config_path)
    monkeypatch.setattr(notifications_cmd.proc, "which", lambda cmd: "/usr/bin/agent-notify")
    monkeypatch.setenv("TEST_TELEGRAM_BOT_TOKEN", "token")
    monkeypatch.setenv("TEST_TELEGRAM_CHAT_ID", "chat")

    rc = notifications_cmd.status(target=tmp_target, profile="operator", json_output=True)
    out = capsys.readouterr().out
    payload = json.loads(out)

    assert rc == 0
    assert payload["installed"] is True
    assert payload["configured"] is True
    assert payload["status"] == "ok"
    assert payload["selected_profile"] == "operator"
    assert payload["selected_channels"] == ["telegram-personal"]


def test_notifications_setup_plan_prints_hook_snippets(tmp_target, capsys):
    rc = notifications_cmd.setup_plan(target=tmp_target, profile="agent-stop")
    out = capsys.readouterr().out

    assert rc == 0
    assert 'notify = ["agent-notify", "--hook", "codex-notify", "--profile", "agent-stop"]' in out
    assert "claude-code-stop --profile agent-stop" in out
    assert "agent-notify has no init or doctor subcommand" not in out
    assert "agent-notify init" not in out


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
    monkeypatch.setattr(notifications_cmd.proc, "which", lambda cmd: "/usr/bin/agent-notify")

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

    health = notifications_cmd.health(tmp_target)
    assert health["latest_event"]["event_type"] == "handoff-waiting"


def test_notifications_event_record_can_explicitly_send(monkeypatch, tmp_target, capsys):
    tmp_target.mkdir()
    config_path = tmp_target / ".config" / "agent-notify" / "config.toml"
    config_path.parent.mkdir(parents=True)
    config_path.write_text(
        "\n".join(
            [
                "[channels.discord-main]",
                'type = "discord"',
                'webhook_url_env = "TEST_DISCORD_WEBHOOK_URL"',
                "",
                "[profiles.operator]",
                'channels = ["discord-main"]',
                "default = true",
                "",
            ]
        )
    )
    seen = {}

    def fake_run(args, timeout=30.0, env=None, cwd=None):
        seen["args"] = args
        seen["cwd"] = cwd
        return notifications_cmd.proc.Result(code=0, stdout="sent", stderr="")

    monkeypatch.setattr(notifications_cmd, "CONFIG_PATH", config_path)
    monkeypatch.setattr(notifications_cmd.proc, "which", lambda cmd: "/usr/bin/agent-notify")
    monkeypatch.setattr(notifications_cmd.proc, "run", fake_run)
    monkeypatch.setenv("TEST_DISCORD_WEBHOOK_URL", "https://example.invalid/hook")

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
    assert "--hook" in seen["args"]
    assert "brigade-event" in seen["args"]
    assert seen["cwd"] == tmp_target.resolve()
