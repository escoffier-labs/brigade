"""Tests for hash-stamped managed blocks and brigade care scheduler scaffold."""

from __future__ import annotations

import json
from pathlib import Path

from brigade import care_cmd, cli, managed_block, memory_cmd


def test_managed_block_round_trip_preserves_neighbor_content():
    host = "# personal crontab\n0 1 * * * /usr/bin/true\n"
    body = "PATH=/home/you/.local/bin:/usr/bin\n15 6 * * * cd /tmp && brigade doctor\n"
    desired = managed_block._normalize_body(body)
    rendered, plan = managed_block.apply_block(host, kind="CARE", profile="crontab", desired_body=body, style="hash")
    assert plan.action == "create"
    assert "# personal crontab" in rendered
    assert "0 1 * * * /usr/bin/true" in rendered
    match = managed_block.find_block(rendered, kind="CARE")
    assert match is not None
    assert match.recorded_hash == managed_block.body_hash(desired)
    assert match.body == desired

    again, plan2 = managed_block.apply_block(rendered, kind="CARE", profile="crontab", desired_body=body, style="hash")
    assert plan2.status == "current"
    assert again == rendered

    removed, removal = managed_block.remove_block(rendered, kind="CARE")
    assert removal.action == "remove"
    assert removed == host


def test_managed_block_reports_tampered_and_refuses_clobber_without_adopt():
    body = "line-a\nline-b"
    wrapped = managed_block.wrap_block(kind="CARE", profile="crontab", body=body, style="hash")
    tampered = wrapped.replace("line-a", "line-EDITED")
    plan = managed_block.classify_block(tampered, kind="CARE", desired_body=body)
    assert plan.status == "tampered"
    unchanged, apply_plan = managed_block.apply_block(
        tampered, kind="CARE", profile="crontab", desired_body=body, style="hash", adopt=False
    )
    assert unchanged == tampered
    assert apply_plan.status == "tampered"
    replaced, adopted = managed_block.apply_block(
        tampered, kind="CARE", profile="crontab", desired_body=body, style="hash", adopt=True
    )
    assert adopted.action == "update"
    assert managed_block.classify_block(replaced, kind="CARE", desired_body=body).status == "current"


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
    monkeypatch.setattr(care_cmd, "_ensure_memory_care_runbooks", lambda target: 0)
    monkeypatch.setattr(care_cmd, "_is_windows", lambda: False)

    target = tmp_path / "ws"
    target.mkdir()
    # Seed memory-care runbooks so status can report presence.
    assert memory_cmd._write_memory_care_runbooks(target) == 0

    assert care_cmd.install(target=target, json_output=True) == 0
    assert "BEGIN BRIGADE CARE" in store["text"]
    assert "keep-me" in store["text"]
    assert "daily-care-pass.json" in store["text"]
    assert "ingest-sweep.json" in store["text"]
    assert "weekly-outcome-ratchet.json" in store["text"]
    assert "15 6 * * *" in store["text"]
    assert "*/30 * * * *" in store["text"]
    assert "0 7 * * 1" in store["text"]
    assert "0 8 * * *" in store["text"]
    assert "0 4 * * *" in store["text"]
    assert "handoff doctor" in store["text"]
    assert "nightly-maintenance.json" in store["text"]

    # Idempotent reinstall is a no-op write path (status current).
    assert care_cmd.install(target=target, json_output=True) == 0

    assert care_cmd.status(target=target, json_output=True) == 0
    # Uninstall restores prior crontab bytes.
    assert care_cmd.uninstall(target=target, json_output=True) == 0
    assert store["text"] == before


def test_care_status_reports_tampered_block_without_clobber(tmp_path, monkeypatch, capsys):
    body = care_cmd._crontab_body(workspace=tmp_path)
    wrapped = managed_block.wrap_block(kind="CARE", profile="crontab", body=body, style="hash")
    store = {"text": wrapped.replace("15 6 * * *", "16 6 * * *")}

    monkeypatch.setattr(care_cmd, "_read_crontab", lambda: (store["text"], None))
    monkeypatch.setattr(
        care_cmd,
        "_write_crontab",
        lambda text: (_ for _ in ()).throw(AssertionError("must not write")),
    )
    monkeypatch.setattr(care_cmd, "_ensure_memory_care_runbooks", lambda target: 0)
    monkeypatch.setattr(care_cmd, "_is_windows", lambda: False)

    assert care_cmd.status(target=tmp_path, json_output=True) == 1
    out = json.loads(capsys.readouterr().out)
    assert out["status"] == "tampered"

    assert care_cmd.install(target=tmp_path, json_output=True) == 1
    assert "16 6 * * *" in store["text"]  # unchanged


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

    assert care_cmd.status(target=tmp_path, json_output=True) == 0
    payload = json.loads(capsys.readouterr().out)
    daily = next(entry for entry in payload["entries"] if entry["id"] == "daily-care")
    assert daily["last_receipt"]["run_id"] == receipt["run_id"]
    assert daily["last_receipt"]["status"] == "completed"


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
    monkeypatch.setattr(care_cmd, "_ensure_memory_care_runbooks", lambda target: 0)
    target = tmp_path / "ws"
    target.mkdir()

    assert care_cmd.install(target=target, backend="systemd", home=home, json_output=True) == 0
    unit_dir = home / ".config" / "systemd" / "user"
    service = unit_dir / "brigade-daily-care.service"
    timer = unit_dir / "brigade-daily-care.timer"
    assert service.is_file()
    assert timer.is_file()
    text = service.read_text(encoding="utf-8")
    assert "BEGIN BRIGADE CARE" in text
    assert "daily-care-pass.json" in text
    assert care_cmd.status(target=target, backend="systemd", home=home, json_output=True) == 0
    assert care_cmd.uninstall(target=target, backend="systemd", home=home, json_output=True) == 0
    assert not service.exists()
    assert not timer.exists()


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
