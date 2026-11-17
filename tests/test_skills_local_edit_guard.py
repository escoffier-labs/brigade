"""A hand-edited installed skill copy is never overwritten by sync, install --force or fleet follow-ups."""

from __future__ import annotations

import json
import shlex
from pathlib import Path

from brigade import cli, skills_cmd
from brigade.skills_cmd import sync_history

SKILL = "edit-guard"
HARNESS = "claude"


def _write_skill(root: Path, body: str = "original") -> Path:
    skill_dir = root / SKILL
    skill_dir.mkdir(parents=True, exist_ok=True)
    (skill_dir / "SKILL.md").write_text(f"# Edit Guard\n\n{body}\n")
    (skill_dir / "CHANGELOG.md").write_text("# 0.1.0\n\n- Initial reviewed workflow.\n")
    (skill_dir / "skill.json").write_text(
        json.dumps(
            {
                "id": SKILL,
                "title": "Edit Guard",
                "version": "0.1.0",
                "required_tools": ["git"],
                "required_mcp_servers": ["github"],
                "supported_harnesses": ["claude", "codex", "cursor"],
                "trust_level": "workspace",
                "tests": ["brigade skills lint edit-guard"],
            }
        )
    )
    return skill_dir


def _installed(workspace: Path) -> Path:
    return workspace / ".claude" / "skills" / SKILL / "SKILL.md"


def _tree_bytes(root: Path) -> dict[str, bytes]:
    return {path.relative_to(root).as_posix(): path.read_bytes() for path in sorted(root.rglob("*")) if path.is_file()}


def _state_bytes(workspace: Path) -> dict[str, bytes]:
    """Every byte sync could touch: the installed copy plus all skills state."""
    state = _tree_bytes(workspace / ".brigade" / "skills")
    live = _tree_bytes(workspace / ".claude" / "skills")
    return {**{f"state/{key}": value for key, value in state.items()}, **{f"live/{key}": v for key, v in live.items()}}


def _rollback_snapshots(workspace: Path) -> list[Path]:
    root = workspace / ".brigade" / "skills" / "rollback"
    return sorted(root.rglob("SKILL.md")) if root.exists() else []


def _edited_workspace(workspace: Path, capsys, harness: str = HARNESS) -> Path:
    """Install a registry skill, hand-edit the copy, then move the registry forward."""
    assert skills_cmd.import_skill(target=workspace, source=_write_skill(workspace / "src1"), json_output=True) == 0
    assert skills_cmd.install(workspace=workspace, skill=f"registry:{SKILL}", harness=harness, json_output=True) == 0
    installed = workspace / f".{harness}" / "skills" / SKILL / "SKILL.md"
    installed.write_text(installed.read_text() + "\nhand edit\n")
    updated = _write_skill(workspace / "src2", body="registry moved on")
    assert skills_cmd.import_skill(target=workspace, source=updated, force=True, json_output=True) == 0
    capsys.readouterr()
    return installed


def _sync(workspace: Path, capsys, *, write: bool, harness: str = HARNESS) -> tuple[int, dict]:
    rc = skills_cmd.sync(workspace=workspace, harness=harness, write=write, json_output=True)
    return rc, json.loads(capsys.readouterr().out)


def _item(payload: dict) -> dict:
    return next(item for item in payload["items"] if item["skill_id"] == SKILL)


def test_sync_dry_run_reports_edited_copy_as_kept_with_diff_and_override(tmp_path, capsys):
    installed = _edited_workspace(tmp_path, capsys)
    before = _state_bytes(tmp_path)

    rc, payload = _sync(tmp_path, capsys, write=False)

    assert rc == 0
    item = _item(payload)
    assert (item["state"], item["action"], item["result"]) == ("edited", "none", "kept")
    assert item["drift"]["local_edit"] == "changed"
    assert "skills diff registry:edit-guard" in item["reason"]
    assert "--against registry" in item["reason"]
    assert "--force --discard-local-edits" in item["reason"]
    assert str(installed.parent) in item["reason"]
    assert payload["counts"]["edited"] == 1
    assert payload["counts"]["changed"] == 0
    assert _state_bytes(tmp_path) == before


def test_sync_write_keeps_edited_copy_without_snapshot_receipt_or_history(tmp_path, capsys):
    installed = _edited_workspace(tmp_path, capsys)
    before = _state_bytes(tmp_path)
    snapshots_before = _rollback_snapshots(tmp_path)

    rc, payload = _sync(tmp_path, capsys, write=True)

    assert rc == 0
    item = _item(payload)
    assert (item["state"], item["result"], item["receipt"]) == ("edited", "kept", None)
    assert payload["applied"] == {"installed": 0, "updated": 0, "restored": 0, "failed": 0}
    assert payload["projection"] is None
    assert "hand edit" in installed.read_text()
    assert _rollback_snapshots(tmp_path) == snapshots_before
    # Installed copy, receipt, history and rollback tree are all byte for byte unchanged.
    assert _state_bytes(tmp_path) == before


def test_sync_write_rechecks_edit_made_between_plan_and_write(tmp_path, capsys, monkeypatch):
    assert skills_cmd.import_skill(target=tmp_path, source=_write_skill(tmp_path / "src1"), json_output=True) == 0
    assert skills_cmd.install(workspace=tmp_path, skill=f"registry:{SKILL}", harness=HARNESS, json_output=True) == 0
    updated = _write_skill(tmp_path / "src2", body="registry moved on")
    assert skills_cmd.import_skill(target=tmp_path, source=updated, force=True, json_output=True) == 0
    capsys.readouterr()
    installed = _installed(tmp_path)
    real_plan = sync_history._sync_plan

    def plan_then_edit(**kwargs):
        result = real_plan(**kwargs)
        # The dry-run view saw a pristine, stale copy; the operator edits afterwards.
        installed.write_text(installed.read_text() + "\nlate hand edit\n")
        return result

    monkeypatch.setattr(sync_history, "_sync_plan", plan_then_edit)
    before_snapshots = _rollback_snapshots(tmp_path)

    rc, payload = _sync(tmp_path, capsys, write=True)

    assert rc == 0
    item = _item(payload)
    assert (item["state"], item["action"], item["result"], item["receipt"]) == ("edited", "none", "kept", None)
    assert "late hand edit" in installed.read_text()
    assert payload["applied"]["updated"] == 0
    assert _rollback_snapshots(tmp_path) == before_snapshots


def test_sync_write_still_updates_pristine_stale_copy_and_snapshots(tmp_path, capsys):
    assert skills_cmd.import_skill(target=tmp_path, source=_write_skill(tmp_path / "src1"), json_output=True) == 0
    assert skills_cmd.install(workspace=tmp_path, skill=f"registry:{SKILL}", harness=HARNESS, json_output=True) == 0
    old_text = _installed(tmp_path).read_text()
    updated = _write_skill(tmp_path / "src2", body="registry moved on")
    assert skills_cmd.import_skill(target=tmp_path, source=updated, force=True, json_output=True) == 0
    capsys.readouterr()

    rc, payload = _sync(tmp_path, capsys, write=True)

    assert rc == 0
    item = _item(payload)
    assert (item["state"], item["result"]) == ("changed", "updated")
    assert "registry moved on" in _installed(tmp_path).read_text()
    snapshots = _rollback_snapshots(tmp_path)
    assert len(snapshots) == 1
    assert snapshots[0].read_text() == old_text


def test_sync_keeps_blocked_state_for_copy_without_receipt(tmp_path, capsys):
    installed = _edited_workspace(tmp_path, capsys)
    (tmp_path / ".brigade" / "skills" / "installs" / f"{SKILL}-{HARNESS}.json").unlink()
    before = _state_bytes(tmp_path)

    rc, payload = _sync(tmp_path, capsys, write=True)

    assert rc == 0
    item = _item(payload)
    assert item["drift"]["local_edit"] == "unknown"
    assert (item["state"], item["action"], item["result"]) == ("blocked", "none", "blocked")
    assert item["reason"] == "installed copy has no valid provenance receipt"
    assert "hand edit" in installed.read_text()
    assert _state_bytes(tmp_path) == before


def test_install_force_refuses_edited_copy_before_any_snapshot_or_removal(tmp_path, capsys):
    installed = _edited_workspace(tmp_path, capsys)
    before = _state_bytes(tmp_path)

    rc = skills_cmd.install(workspace=tmp_path, skill=f"registry:{SKILL}", harness=HARNESS, force=True)

    assert rc == 2
    err = capsys.readouterr().err
    assert str(installed.parent) in err
    assert "skills diff registry:edit-guard" in err
    assert "--against registry" in err
    assert "--force --discard-local-edits" in err
    assert _state_bytes(tmp_path) == before
    assert _rollback_snapshots(tmp_path) == []


def test_install_force_refusal_works_through_the_cli(tmp_path, capsys):
    _edited_workspace(tmp_path, capsys)
    argv = ["skills", "install", f"registry:{SKILL}", "--workspace", str(tmp_path), "--target", HARNESS, "--force"]

    assert cli.main(argv) == 2
    capsys.readouterr()
    assert cli.main([*argv, "--discard-local-edits"]) == 0


def test_install_discard_local_edits_overwrites_and_snapshots_first(tmp_path, capsys):
    installed = _edited_workspace(tmp_path, capsys)
    edited_text = installed.read_text()

    rc = skills_cmd.install(
        workspace=tmp_path,
        skill=f"registry:{SKILL}",
        harness=HARNESS,
        force=True,
        discard_local_edits=True,
        json_output=True,
    )

    assert rc == 0
    capsys.readouterr()
    assert "registry moved on" in installed.read_text()
    assert "hand edit" not in installed.read_text()
    snapshots = _rollback_snapshots(tmp_path)
    assert len(snapshots) == 1
    assert snapshots[0].read_text() == edited_text


def test_install_all_records_edited_copy_as_skipped_and_still_installs_the_rest(tmp_path, capsys):
    installed = _edited_workspace(tmp_path, capsys)
    before = installed.read_bytes()

    rc = skills_cmd.install(workspace=tmp_path, skill=f"registry:{SKILL}", harness="all", force=True, json_output=True)

    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    skipped = [row for row in payload["receipt"]["skipped"] if row["target"] == HARNESS]
    assert len(skipped) == 1 and skipped[0]["local_edit"] is True
    assert "--discard-local-edits" in skipped[0]["reason"]
    assert installed.read_bytes() == before
    assert (tmp_path / ".codex" / "skills" / SKILL / "SKILL.md").exists()
    assert _rollback_snapshots(tmp_path) == []


def test_install_force_still_replaces_copy_without_receipt(tmp_path, capsys):
    installed = _edited_workspace(tmp_path, capsys)
    (tmp_path / ".brigade" / "skills" / "installs" / f"{SKILL}-{HARNESS}.json").unlink()

    rc = skills_cmd.install(workspace=tmp_path, skill=f"registry:{SKILL}", harness=HARNESS, force=True)

    assert rc == 0
    capsys.readouterr()
    assert "registry moved on" in installed.read_text()
    assert len(_rollback_snapshots(tmp_path)) == 1


def test_install_force_still_replaces_pristine_copy(tmp_path, capsys):
    assert skills_cmd.import_skill(target=tmp_path, source=_write_skill(tmp_path / "src1"), json_output=True) == 0
    assert skills_cmd.install(workspace=tmp_path, skill=f"registry:{SKILL}", harness=HARNESS, json_output=True) == 0
    updated = _write_skill(tmp_path / "src2", body="registry moved on")
    assert skills_cmd.import_skill(target=tmp_path, source=updated, force=True, json_output=True) == 0
    capsys.readouterr()

    rc = skills_cmd.install(workspace=tmp_path, skill=f"registry:{SKILL}", harness=HARNESS, force=True)

    assert rc == 0
    capsys.readouterr()
    assert "registry moved on" in _installed(tmp_path).read_text()
    assert len(_rollback_snapshots(tmp_path)) == 1


def test_fleet_status_marks_edited_copy_without_update_command(tmp_path, capsys):
    installed = _edited_workspace(tmp_path, capsys)

    payload = skills_cmd._fleet_status_payload(tmp_path)

    row = next(item for item in payload["copies"] if item["skill_id"] == SKILL)
    assert row["status"] == "edited"
    assert row["drift"]["local_edit"] == "changed"
    assert row["update_command"] is None
    assert row["diff_command"] == shlex.join(
        [
            "brigade",
            "skills",
            "diff",
            f"registry:{SKILL}",
            "--target",
            str(tmp_path),
            "--harness",
            HARNESS,
            "--against",
            "registry",
        ]
    )
    assert row["discard_command"].endswith("--target claude --force --discard-local-edits")
    assert payload["edited_count"] == 1
    assert payload["stale_count"] == 0
    assert payload["stale"] == []
    # Nothing in the machine-readable output offers a bare install --force for this copy.
    forced = [value for value in (row["update_command"], row["diff_command"]) if value and value.endswith("--force")]
    assert forced == []
    assert "hand edit" in installed.read_text()


def test_fleet_status_prints_diff_and_discard_lines_but_no_update_line(tmp_path, capsys):
    _edited_workspace(tmp_path, capsys)

    assert skills_cmd.fleet_status(target=tmp_path) == 0

    out = capsys.readouterr().out
    assert "edited=1" in out
    block = out.split(f"- {SKILL} [{HARNESS}] edited", 1)[1].split("\n- ", 1)[0]
    assert "update:" not in block
    assert "  diff: brigade skills diff" in block
    assert "  discard: brigade skills install" in block
    assert "--discard-local-edits" in block


def test_fleet_discard_command_runs_the_override(tmp_path, capsys):
    installed = _edited_workspace(tmp_path, capsys)
    payload = skills_cmd._fleet_status_payload(tmp_path)
    row = next(item for item in payload["copies"] if item["skill_id"] == SKILL)

    argv = shlex.split(row["discard_command"])
    assert argv[:3] == ["brigade", "skills", "install"]
    assert cli.main(argv[1:]) == 0
    capsys.readouterr()
    assert "registry moved on" in installed.read_text()


def test_fleet_status_keeps_unedited_stale_copy_updatable(tmp_path, capsys):
    assert skills_cmd.import_skill(target=tmp_path, source=_write_skill(tmp_path / "src1"), json_output=True) == 0
    assert skills_cmd.install(workspace=tmp_path, skill=f"registry:{SKILL}", harness=HARNESS, json_output=True) == 0
    updated = _write_skill(tmp_path / "src2", body="registry moved on")
    assert skills_cmd.import_skill(target=tmp_path, source=updated, force=True, json_output=True) == 0
    capsys.readouterr()

    payload = skills_cmd._fleet_status_payload(tmp_path)

    row = next(item for item in payload["copies"] if item["skill_id"] == SKILL)
    assert row["status"] == "stale"
    assert row["update_command"].endswith("--target claude --force")
    assert row["diff_command"] is None and row["discard_command"] is None
    assert payload["edited_count"] == 0


def test_hub_report_keeps_edited_copy_inside_the_closed_status_set(tmp_path, capsys):
    from brigade import fleet_skills_report

    _edited_workspace(tmp_path, capsys)

    rows = [row for row in fleet_skills_report.build_copies(tmp_path) if row["skill_id"] == SKILL]

    assert [row["status"] for row in rows] == ["stale"]
