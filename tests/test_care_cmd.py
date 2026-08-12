"""Tests for hash-stamped managed blocks and brigade care scheduler scaffold."""

from __future__ import annotations

import json
import hashlib
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
