"""Tests for hash-stamped managed blocks and brigade care scheduler scaffold."""

from __future__ import annotations

import json
import hashlib
import sys
from pathlib import Path

import pytest

from brigade import care_cmd, cli, managed_block, memory_cmd


def test_care_registration_identity_is_stable_and_target_specific(tmp_path):
    first = (tmp_path / "first").resolve()
    second = (tmp_path / "second").resolve()
    expected = hashlib.sha256(str(first).encode()).hexdigest()[:16]
    assert care_cmd._target_identity(first) == expected
    assert care_cmd._target_identity(first) != care_cmd._target_identity(second)


def _daily_service(home: Path, target: Path) -> Path:
    identity = care_cmd._target_identity(target)
    return home / ".config" / "systemd" / "user" / f"brigade-care-{identity}-daily-care.service"


def test_care_systemd_uninstall_only_invoking_target(tmp_path, monkeypatch, capsys):
    home = tmp_path / "home"
    first = tmp_path / "first"
    second = tmp_path / "second"
    first.mkdir()
    second.mkdir()
    monkeypatch.setattr(care_cmd, "_is_windows", lambda: False)
    monkeypatch.setattr(care_cmd, "_ensure_care_runbooks", lambda target: 0)

    assert care_cmd.install(target=first, backend="systemd", home=home) == 0
    assert care_cmd.install(target=second, backend="systemd", home=home) == 0
    second_units = set(care_cmd._systemd_unit_bodies(workspace=second, home=home))

    assert care_cmd.uninstall(target=first, backend="systemd", home=home) == 0
    unit_dir = care_cmd._systemd_user_dir(home)
    assert all((unit_dir / name).is_file() for name in second_units)

    assert care_cmd.uninstall(target=first, backend="systemd", home=home) == 0
    assert "no scheduler registration found" in capsys.readouterr().out


def test_care_launchd_registrations_are_target_namespaced(tmp_path, monkeypatch, capsys):
    home = tmp_path / "home"
    first = tmp_path / "first"
    second = tmp_path / "second"
    first.mkdir()
    second.mkdir()
    monkeypatch.setattr(care_cmd, "_ensure_care_runbooks", lambda target: 0)

    assert care_cmd.install(target=first, backend="launchd", home=home) == 0
    assert care_cmd.install(target=second, backend="launchd", home=home) == 0
    second_names = set(care_cmd._launchd_plists(workspace=second, home=home))
    assert care_cmd.uninstall(target=first, backend="launchd", home=home) == 0
    assert all((care_cmd._launchd_dir(home) / name).is_file() for name in second_names)
    assert care_cmd.uninstall(target=first, backend="launchd", home=home) == 0
    assert "no scheduler registration found" in capsys.readouterr().out


def test_care_launchd_weekly_weekday_matches_cron_monday(tmp_path):
    import plistlib

    home = tmp_path / "home"
    target = tmp_path / "ws"
    target.mkdir()
    plists = care_cmd._launchd_plists(workspace=target, home=home)
    weekly_name = next(name for name in plists if "weekly-outcome-ratchet" in name)
    payload = plistlib.loads(plists[weekly_name])
    # cron "0 7 * * 1" and launchd both use 1 = Monday
    assert payload["StartCalendarInterval"] == {"Minute": 0, "Hour": 7, "Weekday": 1}


def test_care_auto_backend_is_explicitly_unsupported_elsewhere(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(care_cmd.sys, "platform", "plan9")
    assert care_cmd.status(target=tmp_path) == 3
    assert "unsupported on plan9" in capsys.readouterr().err


def test_care_managed_block_round_trip_preserves_neighbor_content():
    host = "# personal crontab\n0 1 * * * /usr/bin/true\n"
    body = "PATH=/home/you/.local/bin:/usr/bin\n15 6 * * * cd /tmp && brigade doctor\n"
    plan = managed_block.plan_install(
        host, desired=body, kind="CARE", profile="crontab", style=managed_block.MARKER_STYLE_HASH
    )
    assert plan.action == managed_block.ACTION_CREATE
    rendered = plan.rendered
    assert rendered is not None
    assert "# personal crontab" in rendered
    assert "0 1 * * * /usr/bin/true" in rendered
    assert "# BEGIN BRIGADE CARE" in rendered
    assert "<!-- BEGIN" not in rendered
    parsed = managed_block.parse_blocks(rendered, kind="CARE", style=managed_block.MARKER_STYLE_HASH)
    assert parsed.status == "ok"
    assert parsed.meta is not None
    assert parsed.meta.recorded_hash == managed_block.body_hash(body)
    assert parsed.body == body

    plan2 = managed_block.plan_install(
        rendered, desired=body, kind="CARE", profile="crontab", style=managed_block.MARKER_STYLE_HASH
    )
    assert plan2.status == managed_block.STATUS_CURRENT
    assert plan2.action == managed_block.ACTION_NONE

    removal = managed_block.plan_remove(
        rendered,
        kind="CARE",
        owned_digest=managed_block.body_hash(body),
        style=managed_block.MARKER_STYLE_HASH,
    )
    assert removal.action == managed_block.ACTION_REMOVE
    assert removal.rendered == host


def test_care_managed_block_reports_tampered_and_refuses_clobber_without_adopt():
    body = "line-a\nline-b"
    wrapped = managed_block.render_block(body, kind="CARE", profile="crontab", style=managed_block.MARKER_STYLE_HASH)
    tampered = wrapped.replace("line-a", "line-EDITED")
    assessment = managed_block.assess_block(
        tampered, desired=body, kind="CARE", profile="crontab", style=managed_block.MARKER_STYLE_HASH
    )
    assert assessment.status == managed_block.STATUS_LOCALLY_MODIFIED
    apply_plan = managed_block.plan_install(
        tampered, desired=body, kind="CARE", profile="crontab", adopt=False, style=managed_block.MARKER_STYLE_HASH
    )
    assert apply_plan.status == managed_block.STATUS_LOCALLY_MODIFIED
    assert apply_plan.action == managed_block.ACTION_PRESERVE
    adopted = managed_block.plan_install(
        tampered, desired=body, kind="CARE", profile="crontab", adopt=True, style=managed_block.MARKER_STYLE_HASH
    )
    assert adopted.action == managed_block.ACTION_UPDATE
    assert adopted.rendered is not None
    replaced = adopted.rendered
    assert managed_block.assess_block(
        replaced, desired=body, kind="CARE", profile="crontab", style=managed_block.MARKER_STYLE_HASH
    ).status == (managed_block.STATUS_CURRENT)


def test_care_status_payload_reports_crontab_and_systemd_enabled(tmp_path, monkeypatch):
    """Public structured helper covers both backends without scraping BrigadeCare text."""
    store = {"text": ""}
    home = tmp_path / "home"

    monkeypatch.setattr(care_cmd, "_read_crontab", lambda: (store["text"], None))
    monkeypatch.setattr(care_cmd, "_write_crontab", lambda text: store.__setitem__("text", text) or None)
    monkeypatch.setattr(care_cmd, "_ensure_care_runbooks", lambda target: 0)
    monkeypatch.setattr(care_cmd, "_is_windows", lambda: False)

    target = tmp_path / "ws"
    target.mkdir()
    assert memory_cmd._write_memory_care_runbooks(target) == 0

    missing = care_cmd.status_payload(target=target, home=home)
    assert missing["enabled"] is False
    assert missing["backends"]["crontab"]["status"] == "missing"
    assert missing["backends"]["systemd"]["status"] == "missing"
    assert {entry["id"] for entry in missing["entries"]} >= {
        "daily-care",
        "ingest-sweep",
        "weekly-outcome-ratchet",
        "daily-observability",
        "nightly-ops",
    }

    assert care_cmd.install(target=target, backend="crontab", home=home, json_output=True) == 0
    assert "BrigadeCare" not in store["text"]
    assert "# BEGIN BRIGADE CARE" in store["text"]
    cron_enabled = care_cmd.status_payload(target=target, home=home)
    assert cron_enabled["enabled"] is True
    assert cron_enabled["backends"]["crontab"]["status"] == "current"

    store["text"] = ""
    assert care_cmd.install(target=target, backend="systemd", home=home, json_output=True) == 0
    systemd_enabled = care_cmd.status_payload(target=target, home=home)
    assert systemd_enabled["enabled"] is True
    assert systemd_enabled["backends"]["systemd"]["status"] == "current"


def test_care_status_payload_discovers_per_entry_memory_jobs(tmp_path, monkeypatch):
    """#762 cutover installs MEMORY_JOB entries, not the atomic CARE_ENTRIES set.

    Topology and Center read status_payload; if that helper still evaluates only
    the default five recipes, a successful per-entry migration looks disabled.
    """
    home = tmp_path / "home"
    target = tmp_path / "ws"
    target.mkdir()
    monkeypatch.setattr(care_cmd, "_is_windows", lambda: False)
    monkeypatch.setattr(care_cmd, "_ensure_care_runbooks", lambda target: 0)
    monkeypatch.setattr(care_cmd, "_read_crontab", lambda: ("", None))

    assert (
        care_cmd.install(
            target=target,
            backend="systemd",
            home=home,
            entry_ids=["handoff-ingest", "care-scan", "memory-closeout"],
        )
        == 0
    )

    payload = care_cmd.status_payload(target=target, home=home)
    assert payload["enabled"] is True
    assert payload["target_match"] is True
    assert payload["backends"]["systemd"]["status"] == "current"
    assert payload["backends"]["systemd"]["target_match"] is True
    assert [entry["id"] for entry in payload["entries"]] == [
        "handoff-ingest",
        "care-scan",
        "memory-closeout",
    ]
    for default_id in ("daily-care", "ingest-sweep", "nightly-ops"):
        assert default_id not in {entry["id"] for entry in payload["entries"]}


def test_care_crontab_install_status_uninstall_round_trip(tmp_path, monkeypatch):
    store = {"text": "# keep-me\n0 2 * * * /usr/bin/true\n"}
    before = store["text"]

    def fake_read():
        return store["text"], None

    def fake_write(text: str):
        store["text"] = text
        return None

    monkeypatch.setattr(care_cmd, "_read_crontab", fake_read)
    monkeypatch.setattr(care_cmd, "_write_crontab", fake_write)
    monkeypatch.setattr(care_cmd, "_ensure_care_runbooks", lambda target: 0)
    monkeypatch.setattr(care_cmd, "_is_windows", lambda: False)

    target = tmp_path / "ws"
    target.mkdir()
    # Seed memory-care runbooks so status can report presence.
    assert memory_cmd._write_memory_care_runbooks(target) == 0

    assert care_cmd.install(target=target, backend="crontab", json_output=True) == 0
    assert "# BEGIN BRIGADE CARE" in store["text"]
    assert "<!-- BEGIN" not in store["text"]
    assert "keep-me" in store["text"]
    assert "daily-care-pass.json" in store["text"]
    assert "ingest-sweep.json" in store["text"]
    assert "weekly-outcome-ratchet.json" in store["text"]
    assert "daily-observability.json" in store["text"]
    assert "15 6 * * *" in store["text"]
    assert "*/30 * * * *" in store["text"]
    assert "0 7 * * 1" in store["text"]
    assert "0 8 * * *" in store["text"]
    assert "0 4 * * *" in store["text"]
    assert "nightly-maintenance.json" in store["text"]
    digest = managed_block.parse_blocks(store["text"], kind=care_cmd.CARE_KIND, style=care_cmd.CARE_MARKER_STYLE)
    assert digest.status == "ok"
    assert digest.meta is not None
    assert len(digest.meta.recorded_hash or "") == 64

    # Idempotent reinstall is a no-op write path (status current).
    assert care_cmd.install(target=target, backend="crontab", json_output=True) == 0

    assert care_cmd.status(target=target, backend="crontab", json_output=True) == 0
    # Uninstall restores prior crontab bytes.
    assert care_cmd.uninstall(target=target, backend="crontab", json_output=True) == 0
    assert store["text"] == before


def test_care_status_reports_tampered_block_without_clobber(tmp_path, monkeypatch, capsys):
    body = care_cmd._crontab_body(workspace=tmp_path)
    wrapped = managed_block.render_block(
        body, kind=care_cmd.CARE_KIND, profile="crontab", style=care_cmd.CARE_MARKER_STYLE
    )
    store = {"text": wrapped.replace("15 6 * * *", "16 6 * * *")}

    monkeypatch.setattr(care_cmd, "_read_crontab", lambda: (store["text"], None))
    monkeypatch.setattr(
        care_cmd,
        "_write_crontab",
        lambda text: (_ for _ in ()).throw(AssertionError("must not write")),
    )
    monkeypatch.setattr(care_cmd, "_ensure_care_runbooks", lambda target: 0)
    monkeypatch.setattr(care_cmd, "_is_windows", lambda: False)

    assert care_cmd.status(target=tmp_path, backend="crontab", json_output=True) == 1
    out = json.loads(capsys.readouterr().out)
    assert out["status"] == "tampered"

    assert care_cmd.install(target=tmp_path, backend="crontab", json_output=True) == 1
    assert "16 6 * * *" in store["text"]  # unchanged


def test_care_uninstall_refuses_tampered_crontab_block(tmp_path, monkeypatch, capsys):
    body = care_cmd._crontab_body(workspace=tmp_path)
    wrapped = managed_block.render_block(
        body, kind=care_cmd.CARE_KIND, profile="crontab", style=care_cmd.CARE_MARKER_STYLE
    )
    store = {"text": wrapped.replace("15 6 * * *", "16 6 * * *")}

    monkeypatch.setattr(care_cmd, "_read_crontab", lambda: (store["text"], None))
    monkeypatch.setattr(
        care_cmd,
        "_write_crontab",
        lambda text: (_ for _ in ()).throw(AssertionError("must not write")),
    )
    monkeypatch.setattr(care_cmd, "_is_windows", lambda: False)

    assert care_cmd.uninstall(target=tmp_path, backend="crontab", json_output=True) == 1
    payload = json.loads(capsys.readouterr().out)
    assert payload["status"] == "tampered"
    assert payload["action"] == managed_block.ACTION_PRESERVE
    assert "16 6 * * *" in store["text"]


def test_care_daily_observability_is_runbook_with_receipt_path(tmp_path, monkeypatch):
    monkeypatch.setattr(care_cmd, "_read_crontab", lambda: ("", None))
    monkeypatch.setattr(care_cmd, "_is_windows", lambda: False)

    assert care_cmd._write_daily_observability_runbook(tmp_path) == 0
    entry = next(item for item in care_cmd.CARE_ENTRIES if item.entry_id == "daily-observability")
    assert entry.kind == "runbook"
    assert entry.runbook_id == "daily-observability"
    runbook_path = tmp_path / entry.runbook_rel
    assert runbook_path.is_file()
    payload = json.loads(runbook_path.read_text(encoding="utf-8"))
    assert payload["id"] == "daily-observability"
    assert any("handoff doctor" in step.get("run", "") for step in payload["steps"])


def test_care_status_includes_latest_runbook_receipt(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(care_cmd, "_read_crontab", lambda: ("", None))
    monkeypatch.setattr(care_cmd, "_is_windows", lambda: False)
    runs = tmp_path / ".brigade" / "runbooks" / "runs" / "20260101T000000Z-daily-care-pass"
    runs.mkdir(parents=True)
    receipt = {
        "run_id": "20260101T000000Z-daily-care-pass",
        "runbook_id": "daily-care-pass",
        "status": "completed",
        "started_at": "2026-01-01T00:00:00+00:00",
        "completed_at": "2026-01-01T00:00:01+00:00",
        "receipt_path": str(runs / "receipt.json"),
    }
    (runs / "receipt.json").write_text(json.dumps(receipt), encoding="utf-8")

    assert care_cmd.status(target=tmp_path, backend="crontab", json_output=True) == 0
    payload = json.loads(capsys.readouterr().out)
    daily = next(entry for entry in payload["entries"] if entry["id"] == "daily-care")
    assert daily["last_receipt"]["run_id"] == receipt["run_id"]
    assert daily["last_receipt"]["status"] == "completed"


def test_care_crontab_install_refuses_malformed_markers_without_write(tmp_path, monkeypatch, capsys):
    body = care_cmd._crontab_body(workspace=tmp_path)
    valid = managed_block.render_block(
        body, kind=care_cmd.CARE_KIND, profile="crontab", style=care_cmd.CARE_MARKER_STYLE
    )
    html = managed_block.render_block(
        "orphan-body\n",
        kind=care_cmd.CARE_KIND,
        profile="crontab",
        style=managed_block.MARKER_STYLE_HTML,
    )
    store = {"text": valid + html}

    monkeypatch.setattr(care_cmd, "_read_crontab", lambda: (store["text"], None))
    monkeypatch.setattr(
        care_cmd,
        "_write_crontab",
        lambda text: (_ for _ in ()).throw(AssertionError("must not write")),
    )
    monkeypatch.setattr(care_cmd, "_ensure_care_runbooks", lambda target: 0)
    monkeypatch.setattr(care_cmd, "_is_windows", lambda: False)

    assert care_cmd.install(target=tmp_path, backend="crontab", json_output=True) == 1
    payload = json.loads(capsys.readouterr().out)
    assert payload["status"] == managed_block.STATUS_MALFORMED
    assert payload["action"] == managed_block.ACTION_PRESERVE
    assert payload["detail"]
    assert store["text"] == valid + html


def test_care_crontab_percent_in_path_is_escaped(tmp_path, monkeypatch):
    target = tmp_path / "ws%pct"
    target.mkdir()
    monkeypatch.setattr(care_cmd, "_read_crontab", lambda: ("", None))
    monkeypatch.setattr(care_cmd, "_write_crontab", lambda text: None)
    monkeypatch.setattr(care_cmd, "_ensure_care_runbooks", lambda t: 0)
    monkeypatch.setattr(care_cmd, "_is_windows", lambda: False)

    assert care_cmd.install(target=target, backend="crontab", json_output=True) == 0
    body = care_cmd._crontab_body(workspace=target)
    assert r'cd "ws\%pct"' in body or rf'cd "{target}"'.replace("%", "\\%") in body
    assert "\\%" in body


def test_care_systemd_install_refuses_malformed_unit_without_write(tmp_path, monkeypatch, capsys):
    home = tmp_path / "home"
    monkeypatch.setattr(care_cmd, "_is_windows", lambda: False)
    monkeypatch.setattr(care_cmd, "_ensure_care_runbooks", lambda target: 0)
    target = tmp_path / "ws"
    target.mkdir()

    assert care_cmd.install(target=target, backend="systemd", home=home) == 0
    service = _daily_service(home, target)
    before = service.read_text(encoding="utf-8")
    html = managed_block.render_block(
        "tampered-body\n",
        kind=care_cmd.CARE_KIND,
        profile="systemd",
        style=managed_block.MARKER_STYLE_HTML,
    )
    service.write_text(html, encoding="utf-8")

    capsys.readouterr()
    assert care_cmd.install(target=target, backend="systemd", home=home, json_output=True) == 1
    payload = json.loads(capsys.readouterr().out)
    assert payload["blocked"] is True
    unit = next(item for item in payload["units"] if item["name"] == _daily_service(home, target).name)
    assert unit["status"] == managed_block.STATUS_MALFORMED
    assert unit["action"] == managed_block.ACTION_PRESERVE
    assert unit["detail"]
    assert service.read_text(encoding="utf-8") == html
    assert service.read_text(encoding="utf-8") != before


def test_care_windows_install_prints_schtasks_and_does_not_claim_success(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(care_cmd, "_is_windows", lambda: True)
    calls = []

    def boom(*_a, **_k):
        calls.append(True)
        raise AssertionError("must not touch crontab on Windows")

    monkeypatch.setattr(care_cmd, "_read_crontab", boom)
    monkeypatch.setattr(care_cmd, "_write_crontab", boom)

    rc = care_cmd.install(target=tmp_path)
    assert rc == 3
    out = capsys.readouterr().out
    assert "schtasks" in out
    assert "BrigadeCare-daily-care" in out
    assert calls == []


def _create_line_for(out: str, entry_id: str) -> str:
    marker = f'/TN "BrigadeCare-{entry_id}"'
    for line in out.splitlines():
        if marker in line and line.startswith("schtasks /Create"):
            return line
    raise AssertionError(f"missing schtasks /Create line for {entry_id} in:\n{out}")


def test_care_auto_backend_on_win32_reaches_printed_plan(tmp_path, monkeypatch, capsys):
    """#1021: mocking only `_is_windows` hid that auto never resolved on win32."""
    monkeypatch.setattr(care_cmd.sys, "platform", "win32")
    rc = care_cmd.install(target=tmp_path, backend="auto")
    captured = capsys.readouterr()
    assert rc == 3
    assert "unsupported on win32" not in captured.err
    assert "schtasks /Create" in captured.out
    assert care_cmd._resolve_backend("auto") == care_cmd.SCHTASKS_BACKEND


EXPECTED_SCHTASKS_FLAGS = {
    "15 6 * * *": "/SC DAILY /ST 06:15",
    "*/30 * * * *": "/SC MINUTE /MO 30",
    "0 7 * * 1": "/SC WEEKLY /D MON /ST 07:00",
    "0 8 * * *": "/SC DAILY /ST 08:00",
    "0 4 * * *": "/SC DAILY /ST 04:00",
    "0 7 * * *": "/SC DAILY /ST 07:00",
    "0 9 * * *": "/SC DAILY /ST 09:00",
}


def test_schtasks_schedule_flags_match_every_catalog_hint():
    for entry in (*care_cmd.CARE_ENTRIES, *care_cmd.MEMORY_JOB_ENTRIES):
        assert care_cmd._schtasks_schedule_flags(entry.schedule) == EXPECTED_SCHTASKS_FLAGS[entry.schedule]


def test_schtasks_schedule_flags_fail_closed_on_unsupported_cron():
    with pytest.raises(ValueError, match="unsupported care schedule"):
        care_cmd._schtasks_schedule_flags("0 0 1 * *")
    with pytest.raises(ValueError, match="unsupported care schedule"):
        care_cmd._schtasks_schedule_flags("15 6 * * * *")


def test_care_windows_printed_schtasks_match_each_schedule_hint(tmp_path, monkeypatch, capsys):
    """Regression: the printer used to stamp /SC DAILY /ST 06:15 on every line."""
    monkeypatch.setattr(care_cmd.sys, "platform", "win32")
    assert care_cmd.install(target=tmp_path, backend="auto") == 3
    out = capsys.readouterr().out
    for entry in care_cmd.CARE_ENTRIES:
        line = _create_line_for(out, entry.entry_id)
        flags = EXPECTED_SCHTASKS_FLAGS[entry.schedule]
        assert flags in line, line
        assert f"schedule_hint: cron '{entry.schedule}'" in out
        # The old bug would put 06:15 on ingest-sweep, weekly, observability, nightly.
        if flags != "/SC DAILY /ST 06:15":
            assert "/ST 06:15" not in line
        assert '/RU "%USERNAME%"' in line
        assert "/RL LIMITED" in line
        assert "/NP" in line
        assert line.endswith(" /F")
        assert line.startswith("schtasks /Create ")
        assert '/TR "cmd /c cd /d ' in line


def test_care_windows_printed_path_uses_single_backslashes(tmp_path):
    entry = care_cmd.CARE_ENTRIES[0]
    workspace = Path(r"C:\Users\operator\project")
    command = care_cmd._schtasks_create_command(entry, workspace=workspace)
    assert r"C:\Users\operator\project" in command
    assert r"C:\\Users" not in command
    spaced = care_cmd._schtasks_create_command(entry, workspace=Path(r"C:\Users\operator\my project"))
    assert r'cd /d "C:\Users\operator\my project"' in spaced
    assert r"C:\\Users" not in spaced


def test_care_windows_install_json_returns_structured_plan(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(care_cmd.sys, "platform", "win32")
    assert care_cmd.install(target=tmp_path, backend="auto", json_output=True, dry_run=True) == 3
    captured = capsys.readouterr()
    payload = json.loads(captured.out)
    assert payload["backend"] == "schtasks"
    assert payload["mutates_scheduler"] is False
    assert payload["status"] == "printed-plan"
    assert {task["id"] for task in payload["tasks"]} == {entry.entry_id for entry in care_cmd.CARE_ENTRIES}
    for task, entry in zip(payload["tasks"], care_cmd.CARE_ENTRIES, strict=True):
        assert task["schedule"] == entry.schedule
        assert task["schedule_flags"] == EXPECTED_SCHTASKS_FLAGS[entry.schedule]
        assert task["schedule_flags"] in task["command"]
        assert '/RU "%USERNAME%"' in task["command"]


def test_care_windows_status_reports_task_presence_instead_of_refusing(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(care_cmd.sys, "platform", "win32")

    def fake_query(task_name: str) -> dict:
        if task_name == "BrigadeCare-daily-care":
            return {
                "task_name": task_name,
                "exists": True,
                "status": "Ready",
                "last_run": "8/19/2026 6:15:00 AM",
                "next_run": "8/20/2026 6:15:00 AM",
                "error": None,
            }
        return {
            "task_name": task_name,
            "exists": False,
            "status": "missing",
            "last_run": None,
            "next_run": None,
            "error": None,
        }

    monkeypatch.setattr(care_cmd, "_query_schtasks", fake_query)
    assert care_cmd.status(target=tmp_path, backend="auto", json_output=True) == 0
    captured = capsys.readouterr()
    payload = json.loads(captured.out)
    assert payload["backend"] == "schtasks"
    assert payload["status"] == "partial"
    daily = next(task for task in payload["tasks"] if task["id"] == "daily-care")
    assert daily["exists"] is True
    assert daily["last_run"] == "8/19/2026 6:15:00 AM"
    ingest = next(task for task in payload["tasks"] if task["id"] == "ingest-sweep")
    assert ingest["exists"] is False
    assert "unsupported on win32" not in captured.err


def test_care_status_payload_on_win32_uses_schtasks(tmp_path, monkeypatch):
    monkeypatch.setattr(care_cmd.sys, "platform", "win32")
    monkeypatch.setattr(
        care_cmd,
        "_query_schtasks",
        lambda task_name: {
            "task_name": task_name,
            "exists": False,
            "status": "missing",
            "last_run": None,
            "next_run": None,
            "error": None,
        },
    )
    payload = care_cmd.status_payload(target=tmp_path)
    assert payload["enabled"] is False
    assert payload["backends"]["schtasks"]["status"] == "missing"
    assert payload["backends"]["crontab"]["status"] == "unsupported-backend"
    assert {task["id"] for task in payload["tasks"]} >= {"daily-care", "ingest-sweep"}


def test_care_cli_accepts_schtasks_backend(monkeypatch, tmp_path):
    seen: dict[str, dict] = {}

    def fake_install(**kwargs):
        seen["install"] = kwargs
        return 0

    monkeypatch.setattr("brigade.care_cmd.install", fake_install)
    assert cli.main(["care", "install", "--target", str(tmp_path), "--backend", "schtasks", "--dry-run"]) == 0
    assert seen["install"]["backend"] == "schtasks"


@pytest.mark.skipif(sys.platform != "win32", reason="requires native schtasks.exe")
def test_care_printed_schtasks_commands_are_accepted_by_windows(tmp_path):
    """Native gate: a printed /Create line must be valid schtasks syntax."""
    import subprocess

    rc = care_cmd.install(target=tmp_path, backend="auto")
    assert rc == 3
    # Recreate from the helper so the assertion is the printed command, not a rewrite.
    entry = care_cmd.CARE_ENTRIES[0]
    command = care_cmd._schtasks_create_command(entry, workspace=tmp_path)
    create = subprocess.run(["cmd", "/c", command], capture_output=True, text=True, check=False, timeout=30)
    try:
        assert create.returncode == 0, create.stdout + create.stderr
        query = subprocess.run(
            ["schtasks", "/Query", "/TN", care_cmd._schtasks_task_name(entry), "/FO", "LIST", "/V"],
            capture_output=True,
            text=True,
            check=False,
            timeout=15,
        )
        assert query.returncode == 0, query.stderr
        assert care_cmd._schtasks_task_name(entry) in query.stdout
    finally:
        subprocess.run(
            ["schtasks", "/Delete", "/TN", care_cmd._schtasks_task_name(entry), "/F"],
            capture_output=True,
            text=True,
            check=False,
            timeout=15,
        )


def test_care_cli_dispatch(monkeypatch, tmp_path):
    seen: dict[str, dict] = {}

    def fake_install(**kwargs):
        seen["install"] = kwargs
        return 0

    def fake_status(**kwargs):
        seen["status"] = kwargs
        return 0

    def fake_uninstall(**kwargs):
        seen["uninstall"] = kwargs
        return 0

    monkeypatch.setattr("brigade.care_cmd.install", fake_install)
    monkeypatch.setattr("brigade.care_cmd.status", fake_status)
    monkeypatch.setattr("brigade.care_cmd.uninstall", fake_uninstall)

    assert cli.main(["care", "install", "--target", str(tmp_path), "--backend", "systemd", "--dry-run"]) == 0
    assert seen["install"]["backend"] == "systemd"
    assert seen["install"]["dry_run"] is True
    assert cli.main(["care", "status", "--target", str(tmp_path), "--json"]) == 0
    assert seen["status"]["json_output"] is True
    assert cli.main(["care", "uninstall", "--target", str(tmp_path)]) == 0
    assert seen["uninstall"]["target"] == Path(tmp_path)


def test_care_systemd_install_writes_hash_stamped_units(tmp_path, monkeypatch):
    home = tmp_path / "home"
    monkeypatch.setattr(care_cmd, "_is_windows", lambda: False)
    monkeypatch.setattr(care_cmd, "_ensure_care_runbooks", lambda target: 0)
    target = tmp_path / "ws"
    target.mkdir()

    assert care_cmd.install(target=target, backend="systemd", home=home, json_output=True) == 0
    unit_dir = home / ".config" / "systemd" / "user"
    service = _daily_service(home, target)
    timer = unit_dir / f"brigade-care-{care_cmd._target_identity(target)}-daily-care.timer"
    assert service.is_file()
    assert timer.is_file()
    text = service.read_text(encoding="utf-8")
    assert "# BEGIN BRIGADE CARE" in text
    assert "<!-- BEGIN" not in text
    assert "daily-care-pass.json" in text
    parsed = managed_block.parse_blocks(text, kind=care_cmd.CARE_KIND, style=care_cmd.CARE_MARKER_STYLE)
    assert parsed.status == "ok"
    assert parsed.meta is not None
    assert len(parsed.meta.recorded_hash or "") == 64
    assert care_cmd.status(target=target, backend="systemd", home=home, json_output=True) == 0
    assert care_cmd.uninstall(target=target, backend="systemd", home=home, json_output=True) == 0
    assert not service.exists()
    assert not timer.exists()


def test_care_systemd_unit_quotes_workspace_paths_with_spaces(tmp_path, monkeypatch):
    home = tmp_path / "home"
    monkeypatch.setattr(care_cmd, "_is_windows", lambda: False)
    monkeypatch.setattr(care_cmd, "_ensure_care_runbooks", lambda target: 0)
    target = tmp_path / "my workspace"
    target.mkdir()

    assert care_cmd.install(target=target, backend="systemd", home=home, json_output=True) == 0
    service = _daily_service(home, target)
    text = service.read_text(encoding="utf-8")
    parsed = managed_block.parse_blocks(text, kind=care_cmd.CARE_KIND, style=care_cmd.CARE_MARKER_STYLE)
    assert parsed.status == "ok"
    assert f'WorkingDirectory="{target}"' in parsed.body
    assert (
        "ExecStart=/usr/bin/env brigade runbook run --approved "
        ".brigade/memory-care/runbooks/daily-care-pass.json --target ."
    ) in parsed.body


def test_care_systemd_environment_path_quotes_home_with_spaces(tmp_path, monkeypatch):
    home = tmp_path / "my home"
    monkeypatch.setattr(care_cmd, "_is_windows", lambda: False)
    monkeypatch.setattr(care_cmd, "_ensure_care_runbooks", lambda target: 0)
    target = tmp_path / "ws"
    target.mkdir()

    assert care_cmd.install(target=target, backend="systemd", home=home, json_output=True) == 0
    service = _daily_service(home, target)
    parsed = managed_block.parse_blocks(
        service.read_text(encoding="utf-8"),
        kind=care_cmd.CARE_KIND,
        style=care_cmd.CARE_MARKER_STYLE,
    )
    assert parsed.status == "ok"
    assert 'Environment="PATH=' in parsed.body
    assert str(home / ".local" / "bin") in parsed.body


def test_care_systemd_percent_in_path_is_escaped(tmp_path, monkeypatch):
    home = tmp_path / "pct%home"
    monkeypatch.setattr(care_cmd, "_is_windows", lambda: False)
    monkeypatch.setattr(care_cmd, "_ensure_care_runbooks", lambda target: 0)
    target = tmp_path / "ws"
    target.mkdir()

    assert care_cmd.install(target=target, backend="systemd", home=home, json_output=True) == 0
    service = _daily_service(home, target)
    parsed = managed_block.parse_blocks(
        service.read_text(encoding="utf-8"),
        kind=care_cmd.CARE_KIND,
        style=care_cmd.CARE_MARKER_STYLE,
    )
    assert parsed.status == "ok"
    assert "%%" in parsed.body
    assert 'Environment="PATH=' in parsed.body


def test_care_systemd_uninstall_refuses_tampered_unit(tmp_path, monkeypatch):
    home = tmp_path / "home"
    monkeypatch.setattr(care_cmd, "_is_windows", lambda: False)
    monkeypatch.setattr(care_cmd, "_ensure_care_runbooks", lambda target: 0)
    target = tmp_path / "ws"
    target.mkdir()

    assert care_cmd.install(target=target, backend="systemd", home=home) == 0
    service = _daily_service(home, target)
    tampered = service.read_text(encoding="utf-8").replace("daily-care-pass", "daily-care-TAMPERED")
    service.write_text(tampered, encoding="utf-8")

    assert care_cmd.uninstall(target=target, backend="systemd", home=home) == 1
    assert service.is_file()


def test_care_entries_match_scheduled_care_doc_schedules():
    by_id = {entry.entry_id: entry for entry in care_cmd.CARE_ENTRIES}
    assert by_id["daily-care"].schedule == "15 6 * * *"
    assert by_id["ingest-sweep"].schedule == "*/30 * * * *"
    assert by_id["weekly-outcome-ratchet"].schedule == "0 7 * * 1"
    assert by_id["daily-observability"].schedule == "0 8 * * *"
    assert by_id["nightly-ops"].schedule == "0 4 * * *"
    assert by_id["daily-care"].runbook_rel.endswith("daily-care-pass.json")
    assert by_id["ingest-sweep"].runbook_rel.endswith("ingest-sweep.json")
    assert by_id["weekly-outcome-ratchet"].runbook_rel.endswith("weekly-outcome-ratchet.json")
    assert by_id["daily-observability"].runbook_rel.endswith("daily-observability.json")
    assert by_id["daily-observability"].kind == "runbook"


def test_care_status_payload_different_workspace_does_not_enable_jobs(tmp_path, monkeypatch):
    store = {"text": ""}
    home = tmp_path / "home"
    other = tmp_path / "other-ws"
    other.mkdir()
    target = tmp_path / "ws"
    target.mkdir()

    monkeypatch.setattr(care_cmd, "_read_crontab", lambda: (store["text"], None))
    monkeypatch.setattr(care_cmd, "_write_crontab", lambda text: store.__setitem__("text", text) or None)
    monkeypatch.setattr(care_cmd, "_ensure_care_runbooks", lambda t: 0)
    monkeypatch.setattr(care_cmd, "_is_windows", lambda: False)
    assert memory_cmd._write_memory_care_runbooks(other) == 0
    assert care_cmd.install(target=other, backend="crontab", home=home, json_output=True) == 0

    status = care_cmd.status_payload(target=target, home=home)
    assert status["backends"]["crontab"]["status"] in {"current", "stale", "tampered"}
    assert status["backends"]["crontab"]["target_match"] is False
    assert status["enabled"] is False


def test_care_status_payload_same_workspace_stale_remains_visible(tmp_path, monkeypatch):
    store = {"text": ""}
    home = tmp_path / "home"
    target = tmp_path / "ws"
    target.mkdir()

    monkeypatch.setattr(care_cmd, "_read_crontab", lambda: (store["text"], None))
    monkeypatch.setattr(care_cmd, "_write_crontab", lambda text: store.__setitem__("text", text) or None)
    monkeypatch.setattr(care_cmd, "_ensure_care_runbooks", lambda t: 0)
    monkeypatch.setattr(care_cmd, "_is_windows", lambda: False)
    assert memory_cmd._write_memory_care_runbooks(target) == 0
    assert care_cmd.install(target=target, backend="crontab", home=home, json_output=True) == 0
    # Tamper one schedule line so assessment is stale but still for this workspace.
    store["text"] = store["text"].replace("15 6 * * *", "16 6 * * *")

    status = care_cmd.status_payload(target=target, home=home)
    assert status["backends"]["crontab"]["status"] in {"stale", "tampered"}
    assert status["backends"]["crontab"]["target_match"] is True
    assert status["enabled"] is True


def test_care_systemd_aggregate_status_malformed_beats_missing(tmp_path, monkeypatch):
    home = tmp_path / "home"
    target = tmp_path / "ws"
    target.mkdir()
    monkeypatch.setattr(care_cmd, "_is_windows", lambda: False)
    monkeypatch.setattr(care_cmd, "_ensure_care_runbooks", lambda t: 0)
    monkeypatch.setattr(care_cmd, "_read_crontab", lambda: ("", None))

    assert care_cmd.install(target=target, backend="systemd", home=home, json_output=True) == 0
    # Unit names are target-namespaced (brigade-care-<identity>-...); pre-#898
    # fixed names like brigade-daily-care.service no longer exist.
    service = _daily_service(home, target)
    service.unlink()
    service.symlink_to(home / "missing.service")

    status = care_cmd.status_payload(target=target, home=home)
    assert status["backends"]["systemd"]["status"] == managed_block.STATUS_MALFORMED


def test_care_crontab_block_passes_crontab_syntax_check(tmp_path):
    import shutil
    import subprocess

    if shutil.which("crontab") is None:
        pytest.skip("crontab not available")

    probe = subprocess.run(
        ["crontab", "-n", "/dev/null"],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        check=False,
    )
    if probe.returncode != 0 and "invalid option" in (probe.stderr or "").lower():
        pytest.skip("crontab dry-run validation (-n) is unavailable on this host")

    host = "# neighbor\n0 2 * * * /usr/bin/true\n"
    body = care_cmd._crontab_body(workspace=tmp_path)
    plan = managed_block.plan_install(
        host,
        desired=body,
        kind=care_cmd.CARE_KIND,
        profile="crontab",
        style=care_cmd.CARE_MARKER_STYLE,
    )
    assert plan.rendered is not None
    crontab_file = tmp_path / "probe.crontab"
    crontab_file.write_text(plan.rendered, encoding="utf-8")
    result = subprocess.run(
        ["crontab", "-n", str(crontab_file)],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        check=False,
    )
    assert result.returncode == 0, (result.stdout or "") + (result.stderr or "")


def test_care_crontab_dollar_and_backtick_path_remains_literal(tmp_path):
    workspace = tmp_path / "ws$dollar`tick"
    workspace.mkdir()
    body = care_cmd._crontab_body(workspace=workspace)
    assert "\\$" in body
    assert "\\`" in body
    command = care_cmd._cron_command(care_cmd.CARE_ENTRIES[0], workspace=workspace)
    escaped_workspace = care_cmd._cron_escape_path(str(workspace))
    assert command.startswith(f'cd "{escaped_workspace}" && ')


def test_care_crontab_percent_path_passes_crontab_syntax_check(tmp_path):
    import shutil
    import subprocess

    if shutil.which("crontab") is None:
        pytest.skip("crontab not available")

    probe = subprocess.run(
        ["crontab", "-n", "/dev/null"],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        check=False,
    )
    if probe.returncode != 0 and "invalid option" in (probe.stderr or "").lower():
        pytest.skip("crontab dry-run validation (-n) is unavailable on this host")

    workspace = tmp_path / "ws%pct"
    workspace.mkdir()
    host = "# neighbor\n0 2 * * * /usr/bin/true\n"
    body = care_cmd._crontab_body(workspace=workspace)
    assert "\\%" in body
    plan = managed_block.plan_install(
        host,
        desired=body,
        kind=care_cmd.CARE_KIND,
        profile="crontab",
        style=care_cmd.CARE_MARKER_STYLE,
    )
    assert plan.rendered is not None
    crontab_file = tmp_path / "probe.crontab"
    crontab_file.write_text(plan.rendered, encoding="utf-8")
    result = subprocess.run(
        ["crontab", "-n", str(crontab_file)],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        check=False,
    )
    assert result.returncode == 0, (result.stdout or "") + (result.stderr or "")


def test_care_systemd_status_reports_broken_symlink_as_malformed(tmp_path, monkeypatch, capsys):
    home = tmp_path / "home"
    monkeypatch.setattr(care_cmd, "_is_windows", lambda: False)
    monkeypatch.setattr(care_cmd, "_ensure_care_runbooks", lambda target: 0)
    target = tmp_path / "ws"
    target.mkdir()

    assert care_cmd.install(target=target, backend="systemd", home=home) == 0
    service = _daily_service(home, target)
    service.unlink()
    service.symlink_to(home / "missing.service")
    capsys.readouterr()

    assert care_cmd.status(target=target, backend="systemd", home=home, json_output=True) == 1
    payload = json.loads(capsys.readouterr().out)
    unit = next(item for item in payload["units"] if item["name"] == _daily_service(home, target).name)
    assert unit["status"] == managed_block.STATUS_MALFORMED


def test_care_systemd_uninstall_skips_symlink_swap(tmp_path, monkeypatch):
    home = tmp_path / "home"
    monkeypatch.setattr(care_cmd, "_is_windows", lambda: False)
    monkeypatch.setattr(care_cmd, "_ensure_care_runbooks", lambda target: 0)
    target = tmp_path / "ws"
    target.mkdir()

    assert care_cmd.install(target=target, backend="systemd", home=home) == 0
    service = _daily_service(home, target)
    secret = tmp_path / "secret.service"
    secret.write_text("[Unit]\nDescription=secret\n", encoding="utf-8")
    service.unlink()
    service.symlink_to(secret)

    assert care_cmd.uninstall(target=target, backend="systemd", home=home) == 2
    assert secret.read_text(encoding="utf-8").startswith("[Unit]")
    assert service.is_symlink()


def _unit_path(home: Path, target: Path, entry_id: str, suffix: str) -> Path:
    identity = care_cmd._target_identity(target)
    return home / ".config" / "systemd" / "user" / f"brigade-care-{identity}-{entry_id}{suffix}"


def _write_operator_runbook(target: Path, name: str, runbook_id: str) -> Path:
    path = target / ".brigade" / "memory-care" / "runbooks" / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "id": runbook_id,
                "description": f"Operator-approved {runbook_id} runbook.",
                "allowed_commands": ["brigade"],
                "steps": [{"id": "noop", "run": "brigade --version"}],
            }
        ),
        encoding="utf-8",
    )
    return path


def test_memory_job_catalog_matches_design_doc():
    by_id = {entry.entry_id: entry for entry in care_cmd.MEMORY_JOB_ENTRIES}
    assert list(by_id) == [
        "handoff-ingest",
        "care-scan",
        "memory-refresh",
        "evidence-crawl",
        "memory-closeout",
    ]
    assert by_id["handoff-ingest"].schedule == "*/30 * * * *"
    assert by_id["handoff-ingest"].runbook_rel.endswith("ingest-sweep.json")
    assert by_id["care-scan"].schedule == "15 6 * * *"
    assert by_id["care-scan"].runbook_rel.endswith("daily-care-pass.json")
    assert by_id["memory-refresh"].schedule == "0 7 * * *"
    assert by_id["memory-refresh"].runbook_rel.endswith("memory-refresh.json")
    assert by_id["memory-refresh"].requires_existing_runbook is True
    assert by_id["evidence-crawl"].schedule == "0 8 * * *"
    assert by_id["evidence-crawl"].runbook_rel.endswith("evidence-crawl.json")
    assert by_id["evidence-crawl"].requires_existing_runbook is True
    assert by_id["memory-closeout"].schedule == "0 9 * * *"
    assert by_id["memory-closeout"].runbook_rel.endswith("memory-closeout.json")


def test_care_install_one_entry_does_not_touch_sibling_or_other_target(tmp_path, monkeypatch):
    home = tmp_path / "home"
    first = tmp_path / "first"
    second = tmp_path / "second"
    first.mkdir()
    second.mkdir()
    monkeypatch.setattr(care_cmd, "_is_windows", lambda: False)
    monkeypatch.setattr(care_cmd, "_ensure_care_runbooks", lambda target: 0)

    assert care_cmd.install(target=first, backend="systemd", home=home, entry_ids=["handoff-ingest"]) == 0
    assert care_cmd.install(target=first, backend="systemd", home=home, entry_ids=["care-scan"]) == 0
    assert care_cmd.install(target=second, backend="systemd", home=home, entry_ids=["handoff-ingest"]) == 0

    assert _unit_path(home, first, "handoff-ingest", ".service").is_file()
    assert _unit_path(home, first, "care-scan", ".timer").is_file()
    assert _unit_path(home, second, "handoff-ingest", ".service").is_file()
    assert not _unit_path(home, first, "daily-care", ".service").exists()
    assert not _unit_path(home, first, "nightly-ops", ".timer").exists()
    assert not _unit_path(home, second, "care-scan", ".service").exists()


def test_care_uninstall_one_entry_does_not_touch_sibling_or_other_target(tmp_path, monkeypatch, capsys):
    home = tmp_path / "home"
    first = tmp_path / "first"
    second = tmp_path / "second"
    first.mkdir()
    second.mkdir()
    monkeypatch.setattr(care_cmd, "_is_windows", lambda: False)
    monkeypatch.setattr(care_cmd, "_ensure_care_runbooks", lambda target: 0)

    assert care_cmd.install(target=first, backend="systemd", home=home, entry_ids=["handoff-ingest"]) == 0
    assert care_cmd.install(target=first, backend="systemd", home=home, entry_ids=["care-scan"]) == 0
    assert care_cmd.install(target=second, backend="systemd", home=home, entry_ids=["handoff-ingest"]) == 0

    assert care_cmd.uninstall(target=first, backend="systemd", home=home, entry_ids=["handoff-ingest"]) == 0
    assert not _unit_path(home, first, "handoff-ingest", ".service").exists()
    assert not _unit_path(home, first, "handoff-ingest", ".timer").exists()
    assert _unit_path(home, first, "care-scan", ".service").is_file()
    assert _unit_path(home, second, "handoff-ingest", ".service").is_file()

    capsys.readouterr()
    assert care_cmd.uninstall(target=first, backend="systemd", home=home, entry_ids=["handoff-ingest"]) == 0
    assert "no scheduler registration found" in capsys.readouterr().out
    assert _unit_path(home, first, "care-scan", ".timer").is_file()
    assert _unit_path(home, second, "handoff-ingest", ".timer").is_file()


def test_care_status_reports_named_entry_without_requiring_atomic_set(tmp_path, monkeypatch, capsys):
    home = tmp_path / "home"
    target = tmp_path / "ws"
    target.mkdir()
    monkeypatch.setattr(care_cmd, "_is_windows", lambda: False)
    monkeypatch.setattr(care_cmd, "_ensure_care_runbooks", lambda target: 0)

    assert care_cmd.install(target=target, backend="systemd", home=home, entry_ids=["care-scan"]) == 0
    capsys.readouterr()
    assert care_cmd.status(target=target, backend="systemd", home=home, json_output=True, entry_ids=["care-scan"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["status"] == "current"
    assert [entry["id"] for entry in payload["entries"]] == ["care-scan"]
    unit_names = {item["name"] for item in payload["units"]}
    identity = care_cmd._target_identity(target)
    assert f"brigade-care-{identity}-care-scan.service" in unit_names
    assert f"brigade-care-{identity}-daily-care.service" not in unit_names


@pytest.mark.parametrize("backend", ["systemd", "launchd"])
def test_care_bare_status_enumerates_per_entry_registrations_and_systemd_enablement(
    tmp_path, monkeypatch, capsys, backend
):
    home = tmp_path / "home"
    target = tmp_path / "ws"
    target.mkdir()
    entry_ids = ["handoff-ingest", "care-scan", "memory-closeout"]
    monkeypatch.setattr(care_cmd, "_is_windows", lambda: False)
    monkeypatch.setattr(care_cmd, "_ensure_care_runbooks", lambda target: 0)

    assert care_cmd.status(target=target, backend=backend, home=home, json_output=True) == 0
    missing = json.loads(capsys.readouterr().out)
    assert missing["status"] == "missing"
    assert missing["entries"] == []

    for entry_id in entry_ids:
        assert care_cmd.install(target=target, backend=backend, home=home, entry_ids=[entry_id]) == 0

    capsys.readouterr()
    assert care_cmd.status(target=target, backend=backend, home=home, json_output=True) == 0
    installed = json.loads(capsys.readouterr().out)
    assert installed["status"] == "current"
    assert [entry["id"] for entry in installed["entries"]] == entry_ids
    if backend == "launchd":
        return

    assert installed["enabled"] is False

    wants_dir = care_cmd._systemd_user_dir(home) / "timers.target.wants"
    wants_dir.mkdir(parents=True)
    for entry_id in entry_ids:
        timer = _unit_path(home, target, entry_id, ".timer")
        (wants_dir / timer.name).symlink_to(Path("..") / timer.name)

    assert care_cmd.status(target=target, backend=backend, home=home, json_output=True) == 0
    enabled = json.loads(capsys.readouterr().out)
    assert enabled["status"] == "current"
    assert enabled["enabled"] is True
    assert [entry["id"] for entry in enabled["entries"]] == entry_ids


def test_care_install_without_entry_still_writes_atomic_default_set(tmp_path, monkeypatch):
    home = tmp_path / "home"
    target = tmp_path / "ws"
    target.mkdir()
    monkeypatch.setattr(care_cmd, "_is_windows", lambda: False)
    monkeypatch.setattr(care_cmd, "_ensure_care_runbooks", lambda target: 0)

    assert care_cmd.install(target=target, backend="systemd", home=home) == 0
    for entry_id in ("daily-care", "ingest-sweep", "weekly-outcome-ratchet", "daily-observability", "nightly-ops"):
        assert _unit_path(home, target, entry_id, ".timer").is_file()
    for entry_id in ("handoff-ingest", "care-scan", "memory-refresh", "evidence-crawl", "memory-closeout"):
        assert not _unit_path(home, target, entry_id, ".service").exists()


def test_care_install_memory_refresh_without_runbook_refuses(tmp_path, monkeypatch, capsys):
    home = tmp_path / "home"
    target = tmp_path / "ws"
    target.mkdir()
    monkeypatch.setattr(care_cmd, "_is_windows", lambda: False)
    monkeypatch.setattr(care_cmd, "_ensure_care_runbooks", lambda target: 0)

    assert care_cmd.install(target=target, backend="systemd", home=home, entry_ids=["memory-refresh"]) == 2
    err = capsys.readouterr().err
    assert "memory-refresh" in err
    assert "runbook" in err.lower()
    assert not _unit_path(home, target, "memory-refresh", ".service").exists()
    assert not _unit_path(home, target, "memory-refresh", ".timer").exists()
    assert not list((home / ".config" / "systemd" / "user").glob("brigade-care-*"))


def test_care_install_memory_refresh_with_operator_runbook(tmp_path, monkeypatch):
    home = tmp_path / "home"
    target = tmp_path / "ws"
    target.mkdir()
    _write_operator_runbook(target, "memory-refresh.json", "memory-refresh")
    monkeypatch.setattr(care_cmd, "_is_windows", lambda: False)
    monkeypatch.setattr(care_cmd, "_ensure_care_runbooks", lambda target: 0)

    assert care_cmd.install(target=target, backend="systemd", home=home, entry_ids=["memory-refresh"]) == 0
    service = _unit_path(home, target, "memory-refresh", ".service")
    assert service.is_file()
    text = service.read_text(encoding="utf-8")
    assert "memory-refresh.json" in text
    assert f"brigade-care-{care_cmd._target_identity(target)}-memory-refresh" in service.name


def test_care_unknown_entry_is_error(tmp_path, monkeypatch, capsys):
    home = tmp_path / "home"
    target = tmp_path / "ws"
    target.mkdir()
    monkeypatch.setattr(care_cmd, "_is_windows", lambda: False)
    monkeypatch.setattr(care_cmd, "_ensure_care_runbooks", lambda target: 0)

    assert care_cmd.install(target=target, backend="systemd", home=home, entry_ids=["not-a-job"]) == 2
    assert "unknown care entry" in capsys.readouterr().err
    assert not list((home / ".config" / "systemd" / "user").glob("brigade-care-*"))


def test_care_launchd_uninstall_one_entry_preserves_sibling_and_other_target(tmp_path, monkeypatch):
    home = tmp_path / "home"
    first = tmp_path / "first"
    second = tmp_path / "second"
    first.mkdir()
    second.mkdir()
    monkeypatch.setattr(care_cmd, "_ensure_care_runbooks", lambda target: 0)

    assert care_cmd.install(target=first, backend="launchd", home=home, entry_ids=["handoff-ingest"]) == 0
    assert care_cmd.install(target=first, backend="launchd", home=home, entry_ids=["care-scan"]) == 0
    assert care_cmd.install(target=second, backend="launchd", home=home, entry_ids=["handoff-ingest"]) == 0

    first_ingest = f"dev.brigade.care.{care_cmd._target_identity(first)}.handoff-ingest.plist"
    first_scan = f"dev.brigade.care.{care_cmd._target_identity(first)}.care-scan.plist"
    second_ingest = f"dev.brigade.care.{care_cmd._target_identity(second)}.handoff-ingest.plist"

    assert care_cmd.uninstall(target=first, backend="launchd", home=home, entry_ids=["handoff-ingest"]) == 0
    agents = care_cmd._launchd_dir(home)
    assert not (agents / first_ingest).exists()
    assert (agents / first_scan).is_file()
    assert (agents / second_ingest).is_file()


def test_care_cli_dispatch_passes_entry(monkeypatch, tmp_path):
    seen: dict[str, dict] = {}

    def fake_install(**kwargs):
        seen["install"] = kwargs
        return 0

    def fake_status(**kwargs):
        seen["status"] = kwargs
        return 0

    def fake_uninstall(**kwargs):
        seen["uninstall"] = kwargs
        return 0

    monkeypatch.setattr("brigade.care_cmd.install", fake_install)
    monkeypatch.setattr("brigade.care_cmd.status", fake_status)
    monkeypatch.setattr("brigade.care_cmd.uninstall", fake_uninstall)

    assert (
        cli.main(
            [
                "care",
                "install",
                "--target",
                str(tmp_path),
                "--entry",
                "handoff-ingest",
                "--entry",
                "care-scan",
            ]
        )
        == 0
    )
    assert seen["install"]["entry_ids"] == ["handoff-ingest", "care-scan"]
    assert cli.main(["care", "status", "--target", str(tmp_path), "--entry", "care-scan", "--json"]) == 0
    assert seen["status"]["entry_ids"] == ["care-scan"]
    assert cli.main(["care", "uninstall", "--target", str(tmp_path), "--entry", "handoff-ingest"]) == 0
    assert seen["uninstall"]["entry_ids"] == ["handoff-ingest"]
