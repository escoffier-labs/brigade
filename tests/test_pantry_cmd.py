import json

from brigade import center_cmd, pantry_cmd, work_cmd


def test_pantry_status_reports_uninstalled(monkeypatch, tmp_path):
    monkeypatch.setattr(pantry_cmd.proc, "which", lambda cmd: None)

    payload = pantry_cmd.status_payload(tmp_path)

    assert payload["installed"] is False
    assert payload["health"] == "missing"
    assert "brigade add pantry" in payload["summary"]
    assert "brigade pantry doctor" in payload["next_commands"]


def test_pantry_status_combines_status_and_doctor(monkeypatch, tmp_path):
    monkeypatch.setattr(pantry_cmd.proc, "which", lambda cmd: "/x/agentpantry")

    def fake_run(args, **kw):
        if args == ["agentpantry", "status", "--json"]:
            return pantry_cmd.proc.Result(
                0,
                json.dumps(
                    {
                        "role": "sink",
                        "peer": "127.0.0.1:8787",
                        "surfaces": ["sidecar"],
                        "last_sync": "never",
                        "last_cookies": 0,
                        "last_secrets": 0,
                    }
                ),
                "",
            )
        if args == ["agentpantry", "doctor", "--json"]:
            return pantry_cmd.proc.Result(
                0, json.dumps({"configured": True, "fail_count": 0, "warn_count": 1, "checks": []}), ""
            )
        raise AssertionError(args)

    monkeypatch.setattr(pantry_cmd.proc, "run", fake_run)

    payload = pantry_cmd.status_payload(tmp_path)

    assert payload["installed"] is True
    assert payload["health"] == "warn"
    assert "role=sink" in payload["summary"]
    assert "0 fail/1 warn" in payload["summary"]
    assert any("expiry-alert" in cmd for cmd in payload["next_commands"])


def test_pantry_doctor_exits_nonzero_on_fail(monkeypatch, tmp_path):
    monkeypatch.setattr(pantry_cmd.proc, "which", lambda cmd: "/x/agentpantry")

    def fake_run(args, **kw):
        if args == ["agentpantry", "status", "--json"]:
            return pantry_cmd.proc.Result(
                0,
                json.dumps(
                    {
                        "role": "sink",
                        "peer": "127.0.0.1:8787",
                        "surfaces": ["sidecar"],
                        "last_sync": "never",
                    }
                ),
                "",
            )
        if args == ["agentpantry", "doctor", "--json"]:
            return pantry_cmd.proc.Result(
                1,
                json.dumps(
                    {
                        "configured": True,
                        "fail_count": 1,
                        "warn_count": 0,
                        "checks": [{"status": "FAIL", "name": "key", "detail": "missing"}],
                    }
                ),
                "",
            )
        raise AssertionError(args)

    monkeypatch.setattr(pantry_cmd.proc, "run", fake_run)
    assert pantry_cmd.doctor(target=tmp_path) == 1


def test_setup_plan_is_review_only(tmp_path):
    payload = pantry_cmd.setup_plan_payload(
        target=tmp_path,
        role="sink",
        peer="127.0.0.1:8787",
        config_path="~/.config/agentpantry/config.toml",
        key_path="~/.config/agentpantry/psk.key",
    )

    rendered = pantry_cmd._render_plan_md(payload)

    assert ["agentpantry", "keygen", "--out", "~/.config/agentpantry/psk.key"] in payload["commands"]
    assert "Brigade does not generate or copy PSKs." in payload["boundaries"]
    assert "agentpantry sink --config ~/.config/agentpantry/config.toml" in rendered


def test_setup_plan_write_creates_json_and_markdown(tmp_path):
    rc = pantry_cmd.setup_plan(
        target=tmp_path,
        role="source",
        peer="sink.example:8787",
        write=True,
        json_output=True,
    )

    assert rc == 0
    plans = list((tmp_path / ".brigade" / "pantry" / "plans").glob("*/plan.json"))
    assert len(plans) == 1
    payload = json.loads(plans[0].read_text())
    assert payload["role"] == "source"
    assert "receipt_path" in payload
    assert (plans[0].parent / "PLAN.md").exists()


def test_expiry_alert_plans_notification_without_sending(monkeypatch):
    monkeypatch.setattr(pantry_cmd.proc, "which", lambda cmd: f"/x/{cmd}")

    def fake_run(args, **kw):
        assert args == ["agentpantry", "inventory", "--json", "--expiry-days", "7"]
        return pantry_cmd.proc.Result(
            0,
            json.dumps({"near_expiry": [{"host": "example.com", "name": "sid", "expires": "2026-07-05T00:00:00Z"}]}),
            "",
        )

    monkeypatch.setattr(pantry_cmd.proc, "run", fake_run)

    payload = pantry_cmd.expiry_alert_payload(expiry_days=7, profile="expiry", send=False)

    assert payload["near_expiry_count"] == 1
    assert payload["sent"] is False
    assert payload["planned_argv"][:4] == ["agent-notify", "send", "--profile", "expiry"]
    assert "example.com/sid" in payload["message"]
    assert "brigade pantry expiry-alert --send" in payload["next_commands"]


def test_expiry_alert_send_invokes_agent_notify(monkeypatch):
    monkeypatch.setattr(pantry_cmd.proc, "which", lambda cmd: f"/x/{cmd}")
    calls = []

    def fake_run(args, **kw):
        calls.append((args, kw))
        if args[:2] == ["agentpantry", "inventory"]:
            return pantry_cmd.proc.Result(
                0,
                json.dumps(
                    {"near_expiry": [{"host": "example.com", "name": "sid", "expires": "2026-07-05T00:00:00Z"}]}
                ),
                "",
            )
        if args[:2] == ["agent-notify", "send"]:
            return pantry_cmd.proc.Result(0, "", "")
        raise AssertionError(args)

    monkeypatch.setattr(pantry_cmd.proc, "run", fake_run)

    payload = pantry_cmd.expiry_alert_payload(expiry_days=7, profile="agent-stop", send=True)

    assert payload["sent"] is True
    assert calls[1][0][0:4] == ["agent-notify", "send", "--profile", "agent-stop"]
    assert "example.com/sid" in calls[1][0][4]


def test_work_brief_includes_pantry_health(monkeypatch, tmp_path):
    monkeypatch.setattr(
        pantry_cmd, "status_payload", lambda target: {"installed": False, "summary": "pantry test summary"}
    )

    payload = work_cmd._brief_payload(tmp_path)

    assert payload["pantry"]["summary"] == "pantry test summary"


def test_center_status_includes_pantry_health(monkeypatch, tmp_path):
    monkeypatch.setattr(
        pantry_cmd, "status_payload", lambda target: {"installed": False, "summary": "pantry center summary"}
    )

    payload = center_cmd.status_payload(tmp_path)

    assert payload["pantry"]["summary"] == "pantry center summary"
