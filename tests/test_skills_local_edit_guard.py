"""A hand-edited installed skill copy is never overwritten by sync, install --force or fleet follow-ups."""

from __future__ import annotations

import importlib
import json
import shlex
import shutil
from pathlib import Path

import pytest

from brigade import cli, skills_cmd
from brigade.skills_cmd import sync_history

SKILL = "edit-guard"
HARNESS = "claude"


def _write_skill(
    root: Path,
    body: str = "original",
    skill_id: str = SKILL,
    harnesses: tuple[str, ...] = ("claude", "codex", "cursor"),
) -> Path:
    skill_dir = root / skill_id
    skill_dir.mkdir(parents=True, exist_ok=True)
    (skill_dir / "SKILL.md").write_text(f"# Edit Guard\n\n{body}\n")
    (skill_dir / "CHANGELOG.md").write_text("# 0.1.0\n\n- Initial reviewed workflow.\n")
    (skill_dir / "skill.json").write_text(
        json.dumps(
            {
                "id": skill_id,
                "title": "Edit Guard",
                "version": "0.1.0",
                "required_tools": ["git"],
                "required_mcp_servers": ["github"],
                "supported_harnesses": list(harnesses),
                "trust_level": "workspace",
                "tests": [f"brigade skills lint {skill_id}"],
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


def _history_rows(workspace: Path, harness: str) -> list[str]:
    history = workspace / ".brigade" / "skills" / "installs" / "history.jsonl"
    if not history.exists():
        return []
    return [line for line in history.read_text().splitlines() if json.loads(line).get("target") == harness]


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


def _item(payload: dict, skill_id: str = SKILL) -> dict:
    return next(item for item in payload["items"] if item["skill_id"] == skill_id)


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
    receipt_path = tmp_path / ".brigade" / "skills" / "installs" / f"{SKILL}-{HARNESS}.json"
    receipt_before = receipt_path.read_bytes()
    history_before = _history_rows(tmp_path, HARNESS)

    rc = skills_cmd.install(workspace=tmp_path, skill=f"registry:{SKILL}", harness="all", force=True, json_output=True)

    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    skipped = [row for row in payload["receipt"]["skipped"] if row["target"] == HARNESS]
    assert len(skipped) == 1 and skipped[0]["local_edit"] is True
    assert "--discard-local-edits" in skipped[0]["reason"]
    assert installed.read_bytes() == before
    # The edited target's receipt and history are untouched; only the others were written.
    assert receipt_path.read_bytes() == receipt_before
    assert _history_rows(tmp_path, HARNESS) == history_before
    assert len(_history_rows(tmp_path, "codex")) == 1
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


# ---- rollback: the guard also covers restoring an older snapshot ------------


def _rollbackable_edited_workspace(workspace: Path, capsys) -> tuple[Path, str]:
    """Two installs (so a rollback snapshot exists), then a hand edit of the live copy."""
    assert skills_cmd.import_skill(target=workspace, source=_write_skill(workspace / "src1"), json_output=True) == 0
    assert skills_cmd.install(workspace=workspace, skill=f"registry:{SKILL}", harness=HARNESS, json_output=True) == 0
    first_text = _installed(workspace).read_text()
    updated = _write_skill(workspace / "src2", body="registry moved on")
    assert skills_cmd.import_skill(target=workspace, source=updated, force=True, json_output=True) == 0
    assert skills_cmd.install(workspace=workspace, skill=f"registry:{SKILL}", harness=HARNESS, force=True) == 0
    installed = _installed(workspace)
    installed.write_text(installed.read_text() + "\nhand edit\n")
    capsys.readouterr()
    return installed, first_text


def test_rollback_refuses_edited_copy_before_any_deletion(tmp_path, capsys):
    installed, _first = _rollbackable_edited_workspace(tmp_path, capsys)
    before = _state_bytes(tmp_path)

    rc = skills_cmd.rollback(workspace=tmp_path, skill=SKILL, harness=HARNESS)

    assert rc == 2
    err = capsys.readouterr().err
    assert str(installed.parent) in err
    assert "skills diff" in err
    assert "skills rollback" in err and "--discard-local-edits" in err
    assert "hand edit" in installed.read_text()
    # The installed copy, receipt, history and the rollback snapshot are all untouched.
    assert _state_bytes(tmp_path) == before


def test_rollback_discard_local_edits_snapshots_the_edit_then_rolls_back(tmp_path, capsys):
    installed, first_text = _rollbackable_edited_workspace(tmp_path, capsys)
    edited_text = installed.read_text()

    rc = skills_cmd.rollback(
        workspace=tmp_path, skill=SKILL, harness=HARNESS, discard_local_edits=True, json_output=True
    )

    assert rc == 0
    capsys.readouterr()
    assert installed.read_text() == first_text
    # The consumed snapshot is gone, and the hand edit survives as a new snapshot.
    assert [path.read_text() for path in _rollback_snapshots(tmp_path)] == [edited_text]


def test_rollback_still_works_on_a_pristine_copy_without_the_flag(tmp_path, capsys):
    installed, first_text = _rollbackable_edited_workspace(tmp_path, capsys)
    # Undo the hand edit through the receipt-bound bytes: reinstall pristine, then force a fresh snapshot.
    assert (
        skills_cmd.install(
            workspace=tmp_path,
            skill=f"registry:{SKILL}",
            harness=HARNESS,
            force=True,
            discard_local_edits=True,
        )
        == 0
    )
    capsys.readouterr()
    assert "hand edit" not in installed.read_text()

    assert skills_cmd.rollback(workspace=tmp_path, skill=SKILL, harness=HARNESS) == 0
    capsys.readouterr()
    assert "hand edit" in installed.read_text()
    assert _local_edit(tmp_path) != "unknown"


def test_rollback_flag_works_through_the_cli(tmp_path, capsys):
    installed, first_text = _rollbackable_edited_workspace(tmp_path, capsys)
    argv = ["skills", "rollback", SKILL, "--workspace", str(tmp_path), "--target", HARNESS]

    assert cli.main(argv) == 2
    capsys.readouterr()
    assert cli.main([*argv, "--discard-local-edits"]) == 0
    assert installed.read_text() == first_text


# ---- digest: only the root skill.json is provenance, nested ones are content --


def _nested_workspace(workspace: Path, capsys) -> Path:
    source = _write_skill(workspace / "src1")
    (source / "assets").mkdir()
    (source / "assets" / "skill.json").write_text('{"fixture": 1}\n')
    assert skills_cmd.import_skill(target=workspace, source=source, json_output=True) == 0
    assert skills_cmd.install(workspace=workspace, skill=f"registry:{SKILL}", harness=HARNESS, json_output=True) == 0
    capsys.readouterr()
    return workspace / f".{HARNESS}" / "skills" / SKILL / "assets" / "skill.json"


def _local_edit(workspace: Path) -> str:
    rows = skills_cmd._fleet_status_payload(workspace)["copies"]
    return next(row for row in rows if row["skill_id"] == SKILL)["drift"]["local_edit"]


def _strip_edit_digest(workspace: Path) -> None:
    """Make the receipt look like one written before the digest rule changed."""
    path = workspace / ".brigade" / "skills" / "installs" / f"{SKILL}-{HARNESS}.json"
    receipt = json.loads(path.read_text())
    receipt["installed"].pop("edit_fingerprint", None)
    path.write_text(json.dumps(receipt))


def test_edit_to_nested_skill_json_is_an_edit_and_never_overwritten(tmp_path, capsys):
    nested = _nested_workspace(tmp_path, capsys)
    assert _local_edit(tmp_path) == "current"
    nested.write_text('{"fixture": 2}\n')
    updated = _write_skill(tmp_path / "src2", body="registry moved on")
    (updated / "assets").mkdir()
    (updated / "assets" / "skill.json").write_text('{"fixture": 1}\n')
    assert skills_cmd.import_skill(target=tmp_path, source=updated, force=True, json_output=True) == 0
    capsys.readouterr()
    before = _state_bytes(tmp_path)

    assert _local_edit(tmp_path) == "changed"
    rc, payload = _sync(tmp_path, capsys, write=True)

    assert rc == 0
    assert (_item(payload)["state"], _item(payload)["result"]) == ("edited", "kept")
    assert skills_cmd.install(workspace=tmp_path, skill=f"registry:{SKILL}", harness=HARNESS, force=True) == 2
    assert nested.read_text() == '{"fixture": 2}\n'
    assert _state_bytes(tmp_path) == before


def test_old_receipt_with_nested_skill_json_is_unknown_never_current(tmp_path, capsys):
    _nested_workspace(tmp_path, capsys)
    _strip_edit_digest(tmp_path)

    # The old digest ignored nested skill.json files, so it cannot prove they are pristine.
    assert _local_edit(tmp_path) == "unknown"


def test_old_receipt_without_nested_skill_json_still_compares_as_current(tmp_path, capsys):
    assert skills_cmd.import_skill(target=tmp_path, source=_write_skill(tmp_path / "src1"), json_output=True) == 0
    assert skills_cmd.install(workspace=tmp_path, skill=f"registry:{SKILL}", harness=HARNESS, json_output=True) == 0
    capsys.readouterr()
    _strip_edit_digest(tmp_path)
    assert _local_edit(tmp_path) == "current"

    installed = _installed(tmp_path)
    installed.write_text(installed.read_text() + "\nhand edit\n")
    assert _local_edit(tmp_path) == "changed"


def test_sync_written_receipt_binds_the_same_edit_digest(tmp_path, capsys):
    assert skills_cmd.import_skill(target=tmp_path, source=_write_skill(tmp_path / "src1"), json_output=True) == 0
    capsys.readouterr()
    rc, _payload = _sync(tmp_path, capsys, write=True)
    assert rc == 0
    receipt = json.loads((tmp_path / ".brigade" / "skills" / "installs" / f"{SKILL}-{HARNESS}.json").read_text())

    assert receipt["installed"]["edit_fingerprint"]
    assert _local_edit(tmp_path) == "current"


# ---- sync --write: sibling failure and write-time preconditions --------------


class _FakeReceipt:
    def __init__(self, terminal_state: str) -> None:
        self.terminal_state = terminal_state

    def to_dict(self) -> dict:
        return {"operation_id": "skills-fake", "terminal_state": self.terminal_state}


def test_sync_write_failure_of_a_sibling_does_not_relabel_a_kept_edit_as_restored(tmp_path, capsys, monkeypatch):
    other = "second-skill"
    for skill_id in (SKILL, other):
        assert (
            skills_cmd.import_skill(
                target=tmp_path, source=_write_skill(tmp_path / "src1", skill_id=skill_id), json_output=True
            )
            == 0
        )
        assert (
            skills_cmd.install(workspace=tmp_path, skill=f"registry:{skill_id}", harness=HARNESS, json_output=True) == 0
        )
        updated = _write_skill(tmp_path / "src2", body="registry moved on", skill_id=skill_id)
        assert skills_cmd.import_skill(target=tmp_path, source=updated, force=True, json_output=True) == 0
    capsys.readouterr()
    installed = _installed(tmp_path)
    real_plan = sync_history._sync_plan

    def plan_then_edit(**kwargs):
        result = real_plan(**kwargs)
        installed.write_text(installed.read_text() + "\nlate hand edit\n")
        return result

    monkeypatch.setattr(sync_history, "_sync_plan", plan_then_edit)
    monkeypatch.setattr(sync_history.projection, "execute", lambda plan, target: _FakeReceipt("restored"))

    rc, payload = _sync(tmp_path, capsys, write=True)

    assert rc == 1
    kept = _item(payload)
    assert (kept["state"], kept["action"], kept["result"]) == ("edited", "none", "kept")
    assert kept["drift"]["local_edit"] == "changed"
    assert _item(payload, other)["result"] == "restored"
    assert payload["applied"]["restored"] == 1


def test_sync_write_aborts_when_an_unchanged_file_is_edited_after_planning(tmp_path, capsys, monkeypatch):
    # State-backed copies (the mcp target) keep a fixed mode, so an untouched file
    # produces no mutation of its own: exactly the file whose edit used to slip through.
    harnesses = ("mcp",)
    first = _write_skill(tmp_path / "src1", harnesses=harnesses)
    (first / "notes.md").write_text("reference notes\n")
    assert skills_cmd.import_skill(target=tmp_path, source=first, json_output=True) == 0
    assert skills_cmd.install(workspace=tmp_path, skill=f"registry:{SKILL}", harness="mcp", json_output=True) == 0
    updated = _write_skill(tmp_path / "src2", body="registry moved on", harnesses=harnesses)
    (updated / "notes.md").write_text("reference notes\n")
    assert skills_cmd.import_skill(target=tmp_path, source=updated, force=True, json_output=True) == 0
    capsys.readouterr()
    live = skills_cmd._install_dir(tmp_path, "mcp", SKILL)
    notes = live / "notes.md"
    real_plan = sync_history._sync_projection_plan

    def plan_then_edit(**kwargs):
        result = real_plan(**kwargs)
        assert result[0] is not None
        notes.write_text(notes.read_text() + "edit after planning\n")
        return result

    monkeypatch.setattr(sync_history, "_sync_projection_plan", plan_then_edit)
    receipt_path = tmp_path / ".brigade" / "skills" / "installs" / f"{SKILL}-mcp.json"
    receipt_before = receipt_path.read_bytes()
    live_before = (live / "SKILL.md").read_bytes()

    rc = skills_cmd.sync(workspace=tmp_path, harness="mcp", write=True, json_output=True)

    assert rc == 1
    assert "edit after planning" in notes.read_text()
    assert (live / "SKILL.md").read_bytes() == live_before
    assert receipt_path.read_bytes() == receipt_before
    assert _rollback_snapshots(tmp_path) == []
    assert len(_history_rows(tmp_path, "mcp")) == 1


# ---- SKILL.md gone but other files remain: unknown everywhere ----------------


def test_receipted_copy_missing_skill_md_with_other_files_is_unknown_everywhere(tmp_path, capsys):
    assert skills_cmd.import_skill(target=tmp_path, source=_write_skill(tmp_path / "src1"), json_output=True) == 0
    assert skills_cmd.install(workspace=tmp_path, skill=f"registry:{SKILL}", harness=HARNESS, json_output=True) == 0
    capsys.readouterr()
    installed = _installed(tmp_path)
    installed.unlink()
    assert (installed.parent / "CHANGELOG.md").exists()
    before = _state_bytes(tmp_path)

    row = next(r for r in skills_cmd._fleet_status_payload(tmp_path)["copies"] if r["skill_id"] == SKILL)
    assert (row["status"], row["drift"]["overall"]) == ("unknown", "unknown")
    for write in (False, True):
        rc, payload = _sync(tmp_path, capsys, write=write)
        assert rc == 0
        item = _item(payload)
        assert (item["state"], item["action"], item["result"]) == ("blocked", "none", "blocked")
        assert "SKILL.md" in item["reason"] and "install" in item["reason"]
        assert _state_bytes(tmp_path) == before

    assert skills_cmd.install(workspace=tmp_path, skill=f"registry:{SKILL}", harness=HARNESS, force=True) == 0
    assert installed.exists()


def test_a_copy_with_its_directory_removed_is_still_missing(tmp_path, capsys):
    assert skills_cmd.import_skill(target=tmp_path, source=_write_skill(tmp_path / "src1"), json_output=True) == 0
    assert skills_cmd.install(workspace=tmp_path, skill=f"registry:{SKILL}", harness=HARNESS, json_output=True) == 0
    capsys.readouterr()
    import shutil

    shutil.rmtree(_installed(tmp_path).parent)

    rc, payload = _sync(tmp_path, capsys, write=False)

    assert rc == 0
    assert (_item(payload)["state"], _item(payload)["action"]) == ("missing", "install")


# ---- fleet rows: the same keys on every row ---------------------------------


def test_every_fleet_row_carries_diff_and_discard_keys(tmp_path, capsys):
    assert skills_cmd.import_skill(target=tmp_path, source=_write_skill(tmp_path / "src1"), json_output=True) == 0
    assert skills_cmd.install(workspace=tmp_path, skill=f"registry:{SKILL}", harness=HARNESS, json_output=True) == 0
    assert skills_cmd.install(workspace=tmp_path, skill=f"registry:{SKILL}", harness="codex", json_output=True) == 0
    capsys.readouterr()
    # codex copy becomes a symlink row; the claude copy loses its registry source (unknown row).
    codex_dir = tmp_path / ".codex" / "skills" / SKILL
    import shutil

    shutil.rmtree(codex_dir)
    (tmp_path / "linked-target").mkdir()
    codex_dir.symlink_to(tmp_path / "linked-target")
    shutil.rmtree(tmp_path / ".brigade" / "skills" / "registry" / SKILL)

    rows = skills_cmd._fleet_status_payload(tmp_path)["copies"]

    assert {row["status"] for row in rows} >= {"symlink", "unknown"}
    for row in rows:
        assert "diff_command" in row and "discard_command" in row
        assert row["diff_command"] is None and row["discard_command"] is None


# ---- a copy with no valid receipt: manual commands repair it, reconcile never touches it ----


def _receipt_path(workspace: Path) -> Path:
    return workspace / ".brigade" / "skills" / "installs" / f"{SKILL}-{HARNESS}.json"


def _lose_receipt(workspace: Path, how: str) -> None:
    if how == "deleted":
        _receipt_path(workspace).unlink()
    else:
        _receipt_path(workspace).write_text("{not json")


def test_manual_install_force_still_replaces_a_copy_with_no_valid_receipt(tmp_path, capsys):
    installed = _edited_workspace(tmp_path, capsys)
    _lose_receipt(tmp_path, "deleted")

    assert skills_cmd.install(workspace=tmp_path, skill=f"registry:{SKILL}", harness=HARNESS, force=True) == 0
    assert "hand edit" not in installed.read_text()


# ---- the final mutation is bound to the bytes the check saw -------------------


def _edit_after_first_edit_check(monkeypatch, mutate) -> None:
    """Run ``mutate`` once, right after the first drift check returns and before any write."""
    install_mod = importlib.import_module("brigade.skills_cmd.install")
    real = install_mod._local_edit_state
    seen: list[int] = []

    def checked_then_changed(*args, **kwargs):
        result = real(*args, **kwargs)
        if not seen:
            seen.append(1)
            mutate()
        return result

    monkeypatch.setattr(install_mod, "_local_edit_state", checked_then_changed)


def _late_edit(installed: Path):
    def mutate() -> None:
        installed.write_text(installed.read_text() + "\nedit after the check\n")

    return mutate


def _swap_directory(installed: Path):
    """Replace the installed directory by an identical copy: same bytes, new inode."""

    def mutate() -> None:
        skill_dir = installed.parent
        aside = skill_dir.with_name("swapped-aside")
        skill_dir.rename(aside)
        shutil.copytree(aside, skill_dir)

    return mutate


def _pristine_stale_workspace(workspace: Path, capsys) -> Path:
    assert skills_cmd.import_skill(target=workspace, source=_write_skill(workspace / "src1"), json_output=True) == 0
    assert skills_cmd.install(workspace=workspace, skill=f"registry:{SKILL}", harness=HARNESS, json_output=True) == 0
    updated = _write_skill(workspace / "src2", body="registry moved on")
    assert skills_cmd.import_skill(target=workspace, source=updated, force=True, json_output=True) == 0
    capsys.readouterr()
    return _installed(workspace)


@pytest.mark.parametrize("mutator", [_late_edit, _swap_directory], ids=["file-edit", "directory-swap"])
def test_install_force_aborts_when_the_copy_changes_after_the_edit_check(tmp_path, capsys, monkeypatch, mutator):
    installed = _pristine_stale_workspace(tmp_path, capsys)
    receipt_before = _receipt_path(tmp_path).read_bytes()
    history_before = _history_rows(tmp_path, HARNESS)
    _edit_after_first_edit_check(monkeypatch, mutator(installed))

    rc = skills_cmd.install(workspace=tmp_path, skill=f"registry:{SKILL}", harness=HARNESS, force=True)

    assert rc == 2
    assert "changed" in capsys.readouterr().err
    assert "registry moved on" not in installed.read_text()
    assert _receipt_path(tmp_path).read_bytes() == receipt_before
    assert _history_rows(tmp_path, HARNESS) == history_before
    # The snapshot taken before the aborted removal must not outlive the refusal.
    assert _rollback_snapshots(tmp_path) == []


def test_install_discard_aborts_when_the_copy_changes_after_its_snapshot(tmp_path, capsys, monkeypatch):
    installed = _edited_workspace(tmp_path, capsys)
    registry_mod = importlib.import_module("brigade.skills_cmd.registry")
    real_write = registry_mod._write_collected_tree_into_anchor

    def write_then_edit(*args, **kwargs):
        real_write(*args, **kwargs)
        installed.write_text(installed.read_text() + "\nedit after the snapshot\n")

    monkeypatch.setattr(registry_mod, "_write_collected_tree_into_anchor", write_then_edit)

    rc = skills_cmd.install(
        workspace=tmp_path, skill=f"registry:{SKILL}", harness=HARNESS, force=True, discard_local_edits=True
    )

    assert rc == 2
    assert "edit after the snapshot" in installed.read_text()
    assert "registry moved on" not in installed.read_text()


def _pristine_rollbackable_workspace(workspace: Path, capsys) -> Path:
    installed, _first = _rollbackable_edited_workspace(workspace, capsys)
    assert (
        skills_cmd.install(
            workspace=workspace, skill=f"registry:{SKILL}", harness=HARNESS, force=True, discard_local_edits=True
        )
        == 0
    )
    capsys.readouterr()
    return installed


@pytest.mark.parametrize("mutator", [_late_edit, _swap_directory], ids=["file-edit", "directory-swap"])
def test_rollback_aborts_when_the_copy_changes_after_the_edit_check(tmp_path, capsys, monkeypatch, mutator):
    installed = _pristine_rollbackable_workspace(tmp_path, capsys)
    state_before = {k: v for k, v in _state_bytes(tmp_path).items() if k.startswith("state/")}
    _edit_after_first_edit_check(monkeypatch, mutator(installed))

    rc = skills_cmd.rollback(workspace=tmp_path, skill=SKILL, harness=HARNESS)

    assert rc == 2
    assert "changed" in capsys.readouterr().err
    assert {k: v for k, v in _state_bytes(tmp_path).items() if k.startswith("state/")} == state_before
    assert "registry moved on" in installed.read_text()


def test_rollback_discard_aborts_when_the_copy_changes_after_the_edit_snapshot(tmp_path, capsys, monkeypatch):
    installed, _first = _rollbackable_edited_workspace(tmp_path, capsys)
    state_before = {k: v for k, v in _state_bytes(tmp_path).items() if k.startswith("state/")}
    registry_mod = importlib.import_module("brigade.skills_cmd.registry")
    real_write = registry_mod._write_collected_tree_into_anchor

    def write_then_edit(*args, **kwargs):
        real_write(*args, **kwargs)
        installed.write_text(installed.read_text() + "\nedit after the snapshot\n")

    monkeypatch.setattr(registry_mod, "_write_collected_tree_into_anchor", write_then_edit)

    rc = skills_cmd.rollback(workspace=tmp_path, skill=SKILL, harness=HARNESS, discard_local_edits=True)

    assert rc == 2
    assert "edit after the snapshot" in installed.read_text()
    assert {k: v for k, v in _state_bytes(tmp_path).items() if k.startswith("state/")} == state_before


def test_sync_write_aborts_when_a_file_is_edited_after_the_prepare_check(tmp_path, capsys, monkeypatch):
    installed = _pristine_stale_workspace(tmp_path, capsys)
    receipt_before = _receipt_path(tmp_path).read_bytes()
    history_before = _history_rows(tmp_path, HARNESS)
    kernel = importlib.import_module("brigade.projection.kernel")
    real_at = kernel.FailureInjector.at

    def edit_before_the_first_apply(self, boundary):
        real_at(self, boundary)
        if boundary == "commit:0:before":
            installed.write_text(installed.read_text() + "\nedit after the prepare check\n")

    monkeypatch.setattr(kernel.FailureInjector, "at", edit_before_the_first_apply)

    rc, payload = _sync(tmp_path, capsys, write=True)

    assert rc == 1
    assert payload["projection"]["terminal_state"] == "restored"
    assert "edit after the prepare check" in installed.read_text()
    assert "registry moved on" not in installed.read_text()
    assert _receipt_path(tmp_path).read_bytes() == receipt_before
    assert _history_rows(tmp_path, HARNESS) == history_before
