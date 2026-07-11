from __future__ import annotations

import json
import os
from pathlib import Path

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


def test_notifications_event_record_same_second_events_get_distinct_receipts(monkeypatch, tmp_target, capsys):
    tmp_target.mkdir()
    monkeypatch.setattr(notifications_cmd.proc, "which", lambda cmd: "/usr/bin/agent-notify")
    monkeypatch.setattr(notifications_cmd, "_now", lambda: "2026-01-01T00:00:00+00:00")
    suffixes = iter(["suffix-a", "suffix-b"])
    monkeypatch.setattr(notifications_cmd, "_event_receipt_suffix", lambda: next(suffixes), raising=False)

    payloads = []
    for message in ("First event.", "Second event."):
        assert (
            notifications_cmd.event_record(
                target=tmp_target,
                event_type="operator-alert",
                title="Operator alert",
                message=message,
                json_output=True,
            )
            == 0
        )
        payloads.append(json.loads(capsys.readouterr().out))

    receipts = sorted((tmp_target / ".brigade" / "notifications" / "events").glob("*.json"))

    assert len(payloads) == 2
    assert payloads[0]["event_id"] != payloads[1]["event_id"]
    assert len(receipts) == 2
    assert {json.loads(path.read_text())["message"] for path in receipts} == {"First event.", "Second event."}


def test_notifications_event_record_refuses_symlinked_events_dir_without_external_write(
    monkeypatch, tmp_target, capsys
):
    tmp_target.mkdir()
    external = tmp_target.parent / "external-events"
    external.mkdir()
    events_parent = tmp_target / ".brigade" / "notifications"
    events_parent.mkdir(parents=True)
    (events_parent / "events").symlink_to(external, target_is_directory=True)
    monkeypatch.setattr(notifications_cmd.proc, "which", lambda cmd: "/usr/bin/agent-notify")
    monkeypatch.setattr(notifications_cmd, "_now", lambda: "2026-01-01T00:00:00+00:00")
    monkeypatch.setattr(notifications_cmd, "_event_receipt_suffix", lambda: "suffix-a", raising=False)

    rc = notifications_cmd.event_record(
        target=tmp_target,
        event_type="operator-alert",
        title="Operator alert",
        message="Do not write outside target.",
        json_output=True,
    )

    assert rc == 2
    assert not list(external.iterdir())
    payload = json.loads(capsys.readouterr().out)
    assert payload["error"] == "unsafe notification receipt path"


def test_notifications_event_record_refuses_symlinked_events_dir_before_external_send(monkeypatch, tmp_target, capsys):
    tmp_target.mkdir()
    external = tmp_target.parent / "external-events"
    external.mkdir()
    events_parent = tmp_target / ".brigade" / "notifications"
    events_parent.mkdir(parents=True)
    (events_parent / "events").symlink_to(external, target_is_directory=True)
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
    seen = {"run_called": False}

    def fake_run(args, **kwargs):
        seen["run_called"] = True
        raise AssertionError(args)

    monkeypatch.setattr(notifications_cmd, "CONFIG_PATH", config_path)
    monkeypatch.setattr(notifications_cmd.proc, "which", lambda cmd: "/usr/bin/agent-notify")
    monkeypatch.setattr(notifications_cmd.proc, "run", fake_run)
    monkeypatch.setattr(notifications_cmd, "_now", lambda: "2026-01-01T00:00:00+00:00")
    monkeypatch.setattr(notifications_cmd, "_event_receipt_suffix", lambda: "suffix-a", raising=False)
    monkeypatch.setenv("TEST_DISCORD_WEBHOOK_URL", "https://example.invalid/hook")

    rc = notifications_cmd.event_record(
        target=tmp_target,
        event_type="operator-alert",
        title="Operator alert",
        message="Do not send before proving the receipt path is safe.",
        profile="operator",
        send=True,
        json_output=True,
    )

    assert rc == 2
    assert seen["run_called"] is False
    assert not list(external.iterdir())
    payload = json.loads(capsys.readouterr().out)
    assert payload["error"] == "unsafe notification receipt path"


def test_notifications_event_record_refuses_symlinked_receipt_file_without_external_write(
    monkeypatch, tmp_target, capsys
):
    tmp_target.mkdir()
    external = tmp_target.parent / "external-receipt.json"
    external.write_text("external\n")
    events_root = tmp_target / ".brigade" / "notifications" / "events"
    events_root.mkdir(parents=True)
    receipt_name = "2026-01-01T000000+0000-operator-alert-suffix-a.json"
    (events_root / receipt_name).symlink_to(external)
    monkeypatch.setattr(notifications_cmd.proc, "which", lambda cmd: "/usr/bin/agent-notify")
    monkeypatch.setattr(notifications_cmd, "_now", lambda: "2026-01-01T00:00:00+00:00")
    monkeypatch.setattr(notifications_cmd, "_event_receipt_suffix", lambda: "suffix-a", raising=False)

    rc = notifications_cmd.event_record(
        target=tmp_target,
        event_type="operator-alert",
        title="Operator alert",
        message="Do not follow receipt symlinks.",
        json_output=True,
    )

    assert rc == 2
    assert external.read_text() == "external\n"
    payload = json.loads(capsys.readouterr().out)
    assert payload["error"] == "unsafe notification receipt path"


def test_notifications_event_plan_attaches_bounded_local_evidence_without_writing(monkeypatch, tmp_target, capsys):
    tmp_target.mkdir()
    verify_dir = tmp_target / ".brigade" / "work" / "verify-runs" / "verify-one"
    verify_dir.mkdir(parents=True)
    (verify_dir / "receipt.json").write_text(
        json.dumps(
            {
                "run_id": "verify-one",
                "status": "completed",
                "started_at": "2026-01-01T00:00:00+00:00",
                "completed_at": "2026-01-01T00:00:01+00:00",
                "path": str(verify_dir),
                "commands": [
                    {
                        "command": "pytest -q",
                        "status": "completed",
                        "exit_code": 0,
                        "stdout_summary": f"ok at {tmp_target}/secret-project/tests/test_example.py " + ("x" * 500),
                        "stderr_summary": "",
                    }
                ],
            }
        )
    )
    run_dir = tmp_target / ".brigade" / "runs" / "run-one"
    run_dir.mkdir(parents=True)
    (run_dir / "run.json").write_text(
        json.dumps(
            {
                "status": "ok",
                "task": "Inspect local receipts",
                "started_at": "2026-01-01T00:00:02+00:00",
                "completed_at": "2026-01-01T00:00:03+00:00",
                "artifacts": str(run_dir),
            }
        )
    )
    notification_dir = tmp_target / ".brigade" / "notifications" / "events"
    notification_dir.mkdir(parents=True)
    (notification_dir / "notification-one.json").write_text(
        json.dumps(
            {
                "event_id": "notification-one",
                "event_type": "ci-green",
                "created_at": "2026-01-01T00:00:04+00:00",
                "sent": False,
                "path": str(notification_dir / "notification-one.json"),
            }
        )
    )

    def fake_run(*args, **kwargs):
        raise AssertionError("event plan must not run outbound commands")

    monkeypatch.setattr(notifications_cmd.proc, "which", lambda cmd: "/usr/bin/agent-notify")
    monkeypatch.setattr(notifications_cmd.proc, "run", fake_run)

    rc = notifications_cmd.event_plan(
        target=tmp_target,
        event_type="operator-alert",
        title="Review local evidence",
        message="Evidence is ready.",
        json_output=True,
    )
    payload = json.loads(capsys.readouterr().out)

    assert rc == 0
    assert payload["send_policy"] == "explicit-record-send-only"
    assert payload["send_requested"] is False
    assert payload["sends_notifications"] is False
    assert not Path(payload["receipt_path"]).exists()
    evidence = payload["evidence"]
    assert evidence["attached"] is True
    assert evidence["sources"]["latest_verify"]["run_id"] == "verify-one"
    assert evidence["sources"]["latest_run"]["status"] == "ok"
    assert evidence["sources"]["latest_notification"]["event_id"] == "notification-one"
    rendered = json.dumps(evidence)
    assert str(tmp_target) not in rendered
    assert "secret-project" not in rendered
    assert len(evidence["sources"]["latest_verify"]["commands"][0]["stdout_summary"]) <= 180


def test_notifications_event_no_evidence(monkeypatch, tmp_target, capsys):
    tmp_target.mkdir()
    monkeypatch.setattr(notifications_cmd.proc, "which", lambda cmd: "/usr/bin/agent-notify")

    assert (
        cli.main(
            [
                "notifications",
                "event",
                "plan",
                "--target",
                str(tmp_target),
                "--type",
                "operator-alert",
                "--title",
                "No evidence",
                "--message",
                "Skip receipt summaries.",
                "--no-evidence",
                "--json",
            ]
        )
        == 0
    )
    payload = json.loads(capsys.readouterr().out)
    assert payload["evidence"] == {"attached": False, "disabled": True}


def test_notification_evidence_ignores_external_and_broken_symlinks(tmp_target):
    tmp_target.mkdir()
    root = tmp_target / ".brigade" / "work" / "verify-runs"
    external = tmp_target.parent / "external-receipt.json"
    external.write_text(json.dumps({"run_id": "external-secret", "status": "completed"}))
    linked = root / "linked" / "receipt.json"
    linked.parent.mkdir(parents=True)
    linked.symlink_to(external)
    broken = root / "broken" / "receipt.json"
    broken.parent.mkdir()
    broken.symlink_to(tmp_target.parent / "missing-receipt.json")

    evidence = notifications_cmd._event_evidence(tmp_target.resolve(), enabled=True)

    assert evidence["sources"]["latest_verify"] is None
    assert "external-secret" not in json.dumps(evidence)


def test_notification_evidence_ignores_oversized_receipt(tmp_target):
    tmp_target.mkdir()
    path = tmp_target / ".brigade" / "runs" / "large" / "run.json"
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps({"status": "secret-status", "padding": "x" * 300_000}))

    evidence = notifications_cmd._event_evidence(tmp_target.resolve(), enabled=True)

    assert evidence["sources"]["latest_run"] is None
    assert "secret-status" not in json.dumps(evidence)


def test_notification_evidence_caps_candidates_before_parsing_and_selects_newest_valid(monkeypatch, tmp_target):
    tmp_target.mkdir()
    root = tmp_target / ".brigade" / "work" / "verify-runs"
    for index in range(80):
        path = root / f"bad-{index:03d}" / "receipt.json"
        path.parent.mkdir(parents=True)
        path.write_text("not json")
        os.utime(path, (index + 1, index + 1))
    valid = root / "valid" / "receipt.json"
    valid.parent.mkdir()
    valid.write_text(json.dumps({"run_id": "newest-valid", "status": "completed"}))
    os.utime(valid, (1_000, 1_000))

    reads = 0
    original = notifications_cmd._read_json_object

    def counted_read(path):
        nonlocal reads
        reads += 1
        return original(path)

    monkeypatch.setattr(notifications_cmd, "_read_json_object", counted_read)

    evidence = notifications_cmd._event_evidence(tmp_target.resolve(), enabled=True)

    assert evidence["sources"]["latest_verify"]["run_id"] == "newest-valid"
    assert reads <= notifications_cmd.EVIDENCE_CANDIDATE_LIMIT


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
