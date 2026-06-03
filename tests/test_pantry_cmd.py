import json

from brigade import center_cmd, pantry_cmd, work_cmd


def test_pantry_status_reports_uninstalled(monkeypatch, tmp_path):
    monkeypatch.setattr(pantry_cmd.proc, "which", lambda cmd: None)

    payload = pantry_cmd.status_payload(tmp_path)

    assert payload["installed"] is False
    assert "brigade add pantry" in payload["summary"]


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
            return pantry_cmd.proc.Result(0, json.dumps({"configured": True, "fail_count": 0, "warn_count": 1, "checks": []}), "")
        raise AssertionError(args)

    monkeypatch.setattr(pantry_cmd.proc, "run", fake_run)

    payload = pantry_cmd.status_payload(tmp_path)

    assert payload["installed"] is True
    assert "role=sink" in payload["summary"]
    assert "0 fail/1 warn" in payload["summary"]


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


def test_work_brief_includes_pantry_health(monkeypatch, tmp_path):
    monkeypatch.setattr(pantry_cmd, "status_payload", lambda target: {"installed": False, "summary": "pantry test summary"})

    payload = work_cmd._brief_payload(tmp_path)

    assert payload["pantry"]["summary"] == "pantry test summary"


def test_center_status_includes_pantry_health(monkeypatch, tmp_path):
    monkeypatch.setattr(pantry_cmd, "status_payload", lambda target: {"installed": False, "summary": "pantry center summary"})

    payload = center_cmd.status_payload(tmp_path)

    assert payload["pantry"]["summary"] == "pantry center summary"
