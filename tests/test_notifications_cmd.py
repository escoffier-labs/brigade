from __future__ import annotations

import json

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
    assert any(
        item["source_subsystem"] == "notifications"
        for item in plan_payload["candidate_actions"]
    )
