"""Autonomous reconcile never rolls back, uninstalls or promotes over a hand-edited skill copy."""

from __future__ import annotations

import json
from pathlib import Path

from brigade import outcome, outcome_cmd, skills_cmd
from tests.test_scorecard_reconcile import seed_registry_skill_scorecard_hurt, seed_registry_skill_scorecard_promotion
from tests.test_skills_local_edit_guard import SKILL, _write_skill

CFG = outcome.ReconcileConfig(cooldown_seconds=0)


def _import(tmp_path: Path, capsys) -> None:
    assert skills_cmd.import_skill(target=tmp_path, source=_write_skill(tmp_path / "src"), json_output=True) == 0
    capsys.readouterr()


def _reconcile(tmp_path: Path, capsys) -> dict:
    assert outcome_cmd.reconcile(target=tmp_path, apply=True, config=CFG, json_output=True) == 0
    payload = json.loads(capsys.readouterr().out)
    return next(item for item in payload["decisions"] if item["artifact_id"] == SKILL) | {"applied": payload["applied"]}


def _claude_bytes(workspace: Path) -> dict[str, bytes]:
    """The edited harness's live copy and install receipt (other harnesses may change)."""
    live = workspace / ".claude" / "skills" / SKILL
    paths = [*sorted(live.rglob("*")), workspace / ".brigade" / "skills" / "installs" / f"{SKILL}-claude.json"]
    return {str(path.relative_to(workspace)): path.read_bytes() for path in paths if path.is_file()}


def _status(tmp_path: Path) -> dict:
    return json.loads(outcome_cmd._status_path(tmp_path).read_text())["artifacts"].get(SKILL, {})


def _promoted_then_edited(tmp_path: Path, capsys) -> Path:
    _import(tmp_path, capsys)
    seed_registry_skill_scorecard_promotion(tmp_path, SKILL)
    assert _reconcile(tmp_path, capsys)["execution"] == "installed"
    installed = tmp_path / ".claude" / "skills" / SKILL / "SKILL.md"
    installed.write_text(installed.read_text() + "\nhand edit\n")
    return installed


def test_reconcile_rollback_skips_an_edited_copy_and_never_uninstalls_it(tmp_path, capsys):
    installed = _promoted_then_edited(tmp_path, capsys)
    promoted = _status(tmp_path)
    before = _claude_bytes(tmp_path)
    seed_registry_skill_scorecard_hurt(tmp_path, SKILL)

    decision = _reconcile(tmp_path, capsys)

    assert decision["action"] == "rollback"
    assert "claude:rollback-skipped: local edits" in decision["execution"]
    # A first install has no snapshot, so the old fallback would have uninstalled it.
    assert "hand edit" in installed.read_text()
    assert _claude_bytes(tmp_path) == before
    # Routing demotion is authoritative and not gated on the physical revert
    # (docs/proposals/skill-scorecards.md): the skill is demoted, its route
    # policy is gone, and only the edited files are left in place.
    assert promoted.get("status") == "promoted"
    assert decision["new_status"] == "demoted"
    assert SKILL in decision["applied"]
    assert _status(tmp_path).get("status") == "demoted"
    assert "route_policy" not in _status(tmp_path)


def test_reconcile_rollback_skips_an_edited_copy_even_when_a_snapshot_exists(tmp_path, capsys):
    installed = _promoted_then_edited(tmp_path, capsys)
    # A second pristine install gives the receipt a rollback snapshot; edit again afterwards.
    assert (
        skills_cmd.install(
            workspace=tmp_path,
            skill=f"registry:{SKILL}",
            harness="claude",
            force=True,
            discard_local_edits=True,
            json_output=True,
        )
        == 0
    )
    capsys.readouterr()
    installed.write_text(installed.read_text() + "\nsecond hand edit\n")
    before = _claude_bytes(tmp_path)
    seed_registry_skill_scorecard_hurt(tmp_path, SKILL)

    decision = _reconcile(tmp_path, capsys)

    assert "claude:rollback-skipped: local edits" in decision["execution"]
    assert "second hand edit" in installed.read_text()
    assert _claude_bytes(tmp_path) == before


def test_reconcile_rollback_still_uninstalls_a_pristine_first_install(tmp_path, capsys):
    _import(tmp_path, capsys)
    seed_registry_skill_scorecard_promotion(tmp_path, SKILL)
    assert _reconcile(tmp_path, capsys)["execution"] == "installed"
    seed_registry_skill_scorecard_hurt(tmp_path, SKILL)

    decision = _reconcile(tmp_path, capsys)

    assert decision["new_status"] == "demoted"
    assert "rollback-skipped" not in decision["execution"]
    assert not (tmp_path / ".claude" / "skills" / SKILL).exists()
    assert SKILL in decision["applied"]


def test_reconcile_install_that_installed_nothing_is_not_a_promotion(tmp_path, capsys, monkeypatch):
    _import(tmp_path, capsys)

    def skip_every_target(**_kwargs) -> int:
        # What install(harness="all") reports when every copy is hand-edited: rc 0, installed false.
        skipped = [{"target": t, "reason": "has local edits", "local_edit": True} for t in ("claude", "codex")]
        print(json.dumps({"installed": False, "receipt": {"receipts": [], "skipped": skipped}}))
        return 0

    monkeypatch.setattr(skills_cmd, "install", skip_every_target)
    seed_registry_skill_scorecard_promotion(tmp_path, SKILL)

    decision = _reconcile(tmp_path, capsys)

    assert decision["action"] == "install"
    assert decision["execution"].startswith("install-failed")
    assert "nothing installed" in decision["execution"]
    assert decision["new_status"] == "candidate"
    assert SKILL not in decision["applied"]
    assert "route_policy" not in _status(tmp_path)
    assert _status(tmp_path).get("status") != "promoted"
    receipts = [json.loads(path.read_text()) for path in (tmp_path / "memory" / "outcome" / "decisions").glob("*.json")]
    assert receipts and all("route_policy" not in receipt for receipt in receipts)


def test_reconcile_install_that_installed_somewhere_still_promotes(tmp_path, capsys):
    _import(tmp_path, capsys)
    assert skills_cmd.install(workspace=tmp_path, skill=f"registry:{SKILL}", harness="claude", json_output=True) == 0
    installed = tmp_path / ".claude" / "skills" / SKILL / "SKILL.md"
    installed.write_text(installed.read_text() + "\nhand edit\n")
    capsys.readouterr()
    seed_registry_skill_scorecard_promotion(tmp_path, SKILL)

    decision = _reconcile(tmp_path, capsys)

    assert decision["execution"] == "installed"
    assert decision["new_status"] == "promoted"
    assert "hand edit" in installed.read_text()
    assert (tmp_path / ".codex" / "skills" / SKILL / "SKILL.md").exists()
