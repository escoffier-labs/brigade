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
        if args == ["agentpantry", "version", "--json"]:
            return pantry_cmd.proc.Result(code=0, stdout='{"version": "0.5.0"}', stderr="")
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


def test_pantry_doctor_exits_nonzero_on_fail(monkeypatch, tmp_path, capsys):
    monkeypatch.setattr(pantry_cmd.proc, "which", lambda cmd: "/x/agentpantry")

    def fake_run(args, **kw):
        if args == ["agentpantry", "version", "--json"]:
            return pantry_cmd.proc.Result(code=0, stdout='{"version": "0.5.0"}', stderr="")
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
    assert (
        "note: pantry checks are advisory for workspace doctor; "
        "status 1 occurs for unhealthy, incomplete, or nonzero agentpantry fail_count" in capsys.readouterr().out
    )


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
        if args == ["agentpantry", "version", "--json"]:
            return pantry_cmd.proc.Result(code=0, stdout='{"version": "0.5.0"}', stderr="")
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
        if args == ["agentpantry", "version", "--json"]:
            return pantry_cmd.proc.Result(code=0, stdout='{"version": "0.5.0"}', stderr="")
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
    # calls[0] is the version probe; calls[1] is inventory; calls[2] is agent-notify send.
    assert calls[1][0][0:4] == ["agentpantry", "inventory", "--json", "--expiry-days"]
    assert calls[2][0][0:4] == ["agent-notify", "send", "--profile", "agent-stop"]
    assert "example.com/sid" in calls[2][0][4]


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


def _pantry_installed(monkeypatch):
    monkeypatch.setattr(pantry_cmd.proc, "which", lambda cmd: "/x/agentpantry" if cmd == "agentpantry" else None)


def test_pantry_status_unhealthy_on_below_floor_version(monkeypatch, tmp_path):
    _pantry_installed(monkeypatch)
    calls = []

    def fake_run(args, **kw):
        calls.append(args)
        assert args == ["agentpantry", "version", "--json"]
        return pantry_cmd.proc.Result(code=0, stdout='{"version": "0.4.1"}', stderr="")

    monkeypatch.setattr(pantry_cmd.proc, "run", fake_run)

    payload = pantry_cmd.status_payload(tmp_path)

    assert payload["installed"] is True
    assert payload["health"] == "unhealthy"
    assert payload["version"] == "0.4.1"
    assert payload["version_compatible"] is False
    assert "expected >= 0.5.0" in payload["summary"]
    assert "0.4.1" in payload["summary"]
    assert payload["status"] is None
    assert payload["doctor"] is None
    # No downstream status/doctor surface invoked after incompatibility.
    assert calls == [["agentpantry", "version", "--json"]]


def test_pantry_doctor_nonzero_on_below_floor_version(monkeypatch, tmp_path):
    _pantry_installed(monkeypatch)
    calls = []

    def fake_run(args, **kw):
        calls.append(args)
        assert args == ["agentpantry", "version", "--json"]
        return pantry_cmd.proc.Result(code=0, stdout='{"version": "0.4.1"}', stderr="")

    monkeypatch.setattr(pantry_cmd.proc, "run", fake_run)

    assert pantry_cmd.doctor(target=tmp_path) == 1
    assert calls == [["agentpantry", "version", "--json"]]


def test_pantry_status_unhealthy_on_nonzero_version_probe(monkeypatch, tmp_path):
    _pantry_installed(monkeypatch)
    calls = []

    def fake_run(args, **kw):
        calls.append(args)
        assert args == ["agentpantry", "version", "--json"]
        return pantry_cmd.proc.Result(code=1, stdout="", stderr="version flag not defined")

    monkeypatch.setattr(pantry_cmd.proc, "run", fake_run)

    payload = pantry_cmd.status_payload(tmp_path)

    assert payload["health"] == "unhealthy"
    assert "expected >= 0.5.0" in payload["summary"]
    assert "probe exit 1" in payload["summary"]
    assert calls == [["agentpantry", "version", "--json"]]


def test_pantry_status_unhealthy_on_malformed_version_json(monkeypatch, tmp_path):
    _pantry_installed(monkeypatch)
    calls = []

    def fake_run(args, **kw):
        calls.append(args)
        assert args == ["agentpantry", "version", "--json"]
        return pantry_cmd.proc.Result(code=0, stdout="totally-not-json", stderr="")

    monkeypatch.setattr(pantry_cmd.proc, "run", fake_run)

    payload = pantry_cmd.status_payload(tmp_path)

    assert payload["health"] == "unhealthy"
    assert "expected >= 0.5.0" in payload["summary"]
    assert calls == [["agentpantry", "version", "--json"]]


def test_pantry_status_unhealthy_on_dev_version(monkeypatch, tmp_path):
    _pantry_installed(monkeypatch)
    calls = []

    def fake_run(args, **kw):
        calls.append(args)
        assert args == ["agentpantry", "version", "--json"]
        return pantry_cmd.proc.Result(code=0, stdout='{"version": "dev"}', stderr="")

    monkeypatch.setattr(pantry_cmd.proc, "run", fake_run)

    payload = pantry_cmd.status_payload(tmp_path)

    assert payload["health"] == "unhealthy"
    # An unparsable version string collapses to the fixed sanitized label and
    # never leaks the raw value into the surfaced pantry payload.
    assert payload["version"] == "invalid-version"
    assert "expected >= 0.5.0" in payload["summary"]
    assert "invalid-version" in payload["summary"]
    assert "dev" not in payload["summary"]
    assert "dev" not in payload["version"]
    assert calls == [["agentpantry", "version", "--json"]]


def test_pantry_status_never_leaks_secret_version_field(monkeypatch, tmp_path):
    _pantry_installed(monkeypatch)
    secret = "AKIA-DEADFAKE-SECRET-KEY-DO-NOT-LEAK"

    def fake_run(args, **kw):
        assert args == ["agentpantry", "version", "--json"]
        return pantry_cmd.proc.Result(code=0, stdout=json.dumps({"version": secret}), stderr="")

    monkeypatch.setattr(pantry_cmd.proc, "run", fake_run)

    payload = pantry_cmd.status_payload(tmp_path)

    assert payload["health"] == "unhealthy"
    assert payload["version"] == "invalid-version"
    assert secret not in payload["version"]
    assert secret not in payload["summary"]
    assert "invalid-version" in payload["summary"]


def test_pantry_status_unhealthy_on_missing_version_field(monkeypatch, tmp_path):
    _pantry_installed(monkeypatch)
    calls = []

    def fake_run(args, **kw):
        calls.append(args)
        assert args == ["agentpantry", "version", "--json"]
        return pantry_cmd.proc.Result(code=0, stdout='{"other": "field"}', stderr="")

    monkeypatch.setattr(pantry_cmd.proc, "run", fake_run)

    payload = pantry_cmd.status_payload(tmp_path)

    assert payload["health"] == "unhealthy"
    assert payload["version"] == "missing"
    assert "expected >= 0.5.0" in payload["summary"]
    assert calls == [["agentpantry", "version", "--json"]]


def test_pantry_status_compatible_above_floor_multi_digit(monkeypatch, tmp_path):
    _pantry_installed(monkeypatch)

    def fake_run(args, **kw):
        if args == ["agentpantry", "version", "--json"]:
            return pantry_cmd.proc.Result(code=0, stdout='{"version": "v0.10.3"}', stderr="")
        if args == ["agentpantry", "status", "--json"]:
            return pantry_cmd.proc.Result(
                0,
                json.dumps({"role": "sink", "peer": "127.0.0.1:8787", "surfaces": [], "last_sync": "never"}),
                "",
            )
        if args == ["agentpantry", "doctor", "--json"]:
            return pantry_cmd.proc.Result(
                0, json.dumps({"configured": True, "fail_count": 0, "warn_count": 0, "checks": []}), ""
            )
        raise AssertionError(args)

    monkeypatch.setattr(pantry_cmd.proc, "run", fake_run)

    payload = pantry_cmd.status_payload(tmp_path)

    assert payload["version_compatible"] is True
    assert payload["version"] == "0.10.3"
    assert payload["health"] == "ok"


def test_expiry_alert_returns_structured_failure_on_incompatibility(monkeypatch):
    _pantry_installed(monkeypatch)
    calls = []

    def fake_run(args, **kw):
        calls.append(args)
        assert args == ["agentpantry", "version", "--json"]
        return pantry_cmd.proc.Result(code=0, stdout='{"version": "0.4.1"}', stderr="")

    monkeypatch.setattr(pantry_cmd.proc, "run", fake_run)

    payload = pantry_cmd.expiry_alert_payload(expiry_days=7, profile="agent-stop", send=False)

    assert payload["version"] == "0.4.1"
    assert payload["version_compatible"] is False
    assert "expected >= 0.5.0" in payload["error"]
    assert "0.4.1" in payload["error"]
    assert "inventory" not in payload
    assert payload["near_expiry_count"] == 0
    assert payload["sent"] is False
    # No downstream inventory surface invoked after incompatibility.
    assert calls == [["agentpantry", "version", "--json"]]


def test_expiry_alert_structured_failure_on_nonzero_probe(monkeypatch):
    _pantry_installed(monkeypatch)
    calls = []

    def fake_run(args, **kw):
        calls.append(args)
        assert args == ["agentpantry", "version", "--json"]
        return pantry_cmd.proc.Result(code=2, stdout="", stderr="flag not defined")

    monkeypatch.setattr(pantry_cmd.proc, "run", fake_run)

    payload = pantry_cmd.expiry_alert_payload(expiry_days=14, profile="agent-stop", send=True)

    assert payload["version_compatible"] is False
    assert "expected >= 0.5.0" in payload["error"]
    assert "probe exit 2" in payload["error"]
    assert "inventory" not in payload
    assert payload["sent"] is False
    assert calls == [["agentpantry", "version", "--json"]]
