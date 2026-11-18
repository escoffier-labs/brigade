"""Autonomous reconcile is routing only: it never writes a skill file (#2589, owner decision 2026-11-18).

Every local input to a physical change (install receipts, rollback snapshot
pointers and fingerprints, the registry) is writable by the agent being gated,
so a scheduled install or rollback could install bytes the agent chose.
Reconcile therefore sets status and the route policy marker and reports the
operator command that would apply the physical change; it runs none of them.
"""

from __future__ import annotations

import json
import shlex
from pathlib import Path

from brigade import outcome, outcome_cmd, outcome_ratchet_report, outcome_skill_exec, skills_cmd
from brigade.skills_cmd import registry
from tests.test_scorecard_reconcile import seed_registry_skill_scorecard_hurt, seed_registry_skill_scorecard_promotion
from tests.test_skills_local_edit_guard import SKILL, _write_skill

CFG = outcome.ReconcileConfig(cooldown_seconds=0)
# Reconcile's own routing state; everything else under the workspace must stay byte-identical.
_ROUTING_STATE = ("memory", ".brigade/memory-care")


def _import(tmp_path: Path, capsys) -> None:
    assert skills_cmd.import_skill(target=tmp_path, source=_write_skill(tmp_path / "src"), json_output=True) == 0
    capsys.readouterr()


def _install_all(tmp_path: Path, capsys) -> None:
    for harness in ("claude", "codex"):
        assert skills_cmd.install(workspace=tmp_path, skill=f"registry:{SKILL}", harness=harness, json_output=True) == 0
    capsys.readouterr()


def _reconcile(tmp_path: Path, capsys, *, report: bool = False) -> dict:
    assert outcome_cmd.reconcile(target=tmp_path, apply=True, config=CFG, json_output=True, report=report) == 0
    payload = json.loads(capsys.readouterr().out)
    return next(item for item in payload["decisions"] if item["artifact_id"] == SKILL) | {"applied": payload["applied"]}


def _disk(tmp_path: Path) -> dict[str, tuple[bytes, int]]:
    """Every file under the workspace except reconcile's routing state, with its mode."""
    skipped = tuple(tmp_path / part for part in _ROUTING_STATE)
    return {
        path.relative_to(tmp_path).as_posix(): (path.read_bytes(), path.stat().st_mode)
        for path in sorted(tmp_path.rglob("*"))
        if path.is_file() and not any(path.is_relative_to(root) for root in skipped)
    }


def _status(tmp_path: Path) -> dict:
    return json.loads(outcome_cmd._status_path(tmp_path).read_text())["artifacts"].get(SKILL, {})


def _commands(decision: dict) -> dict[str, list[str]]:
    """Pending operator commands keyed by harness, parsed so the assertions read the argv."""
    parsed: dict[str, list[str]] = {}
    for command in decision.get("pending_commands", []):
        argv = shlex.split(command)
        parsed[argv[argv.index("--target") + 1]] = argv
    return parsed


def test_promotion_sets_status_and_route_policy_without_touching_any_file(tmp_path, capsys):
    _import(tmp_path, capsys)
    seed_registry_skill_scorecard_promotion(tmp_path, SKILL)
    before = _disk(tmp_path)

    decision = _reconcile(tmp_path, capsys)

    assert decision["action"] == "install"
    assert decision["execution"] == "routing-only"
    assert decision["new_status"] == "promoted"
    assert SKILL in decision["applied"]
    assert _status(tmp_path)["status"] == "promoted"
    assert "route_policy" in _status(tmp_path)
    assert _disk(tmp_path) == before
    assert not (tmp_path / ".claude" / "skills" / SKILL).exists()
    assert not (tmp_path / ".brigade" / "skills" / "installs").exists()
    receipts = [json.loads(p.read_text()) for p in (tmp_path / "memory" / "outcome" / "decisions").glob("*.json")]
    assert receipts and all(r["execution"] == "routing-only" for r in receipts)


def test_promotion_lists_the_install_command_for_every_missing_copy(tmp_path, capsys):
    _import(tmp_path, capsys)
    seed_registry_skill_scorecard_promotion(tmp_path, SKILL)

    decision = _reconcile(tmp_path, capsys)

    commands = _commands(decision)
    assert {"claude", "codex"} <= set(commands)
    for harness, argv in commands.items():
        assert argv[:7] == ["brigade", "skills", "install", SKILL, "--target", harness, "--force"]
        assert "--discard-local-edits" not in argv
        assert argv[-2:] == ["--workspace", str(tmp_path.resolve())]
    # The same commands are in the decision receipt, for the operator reading history.
    receipts = [json.loads(p.read_text()) for p in (tmp_path / "memory" / "outcome" / "decisions").glob("*.json")]
    assert receipts[0]["pending_commands"] == decision["pending_commands"]


def test_promotion_lists_only_stale_or_missing_copies(tmp_path, capsys):
    _import(tmp_path, capsys)
    _install_all(tmp_path, capsys)
    # claude stays current, codex goes stale (the registry moved on), cursor was never installed.
    updated = _write_skill(tmp_path / "src2", body="registry moved on")
    assert skills_cmd.import_skill(target=tmp_path, source=updated, force=True, json_output=True) == 0
    assert skills_cmd.install(workspace=tmp_path, skill=f"registry:{SKILL}", harness="claude", force=True) == 0
    capsys.readouterr()
    seed_registry_skill_scorecard_promotion(tmp_path, SKILL)
    before = _disk(tmp_path)

    decision = _reconcile(tmp_path, capsys)

    commands = _commands(decision)
    assert "claude" not in commands
    assert {"codex", "cursor"} <= set(commands)
    assert _disk(tmp_path) == before


def test_promotion_never_offers_a_command_for_a_hand_edited_copy(tmp_path, capsys):
    _import(tmp_path, capsys)
    _install_all(tmp_path, capsys)
    installed = tmp_path / ".claude" / "skills" / SKILL / "SKILL.md"
    installed.write_text(installed.read_text() + "\nhand edit\n")
    seed_registry_skill_scorecard_promotion(tmp_path, SKILL)
    before = _disk(tmp_path)

    decision = _reconcile(tmp_path, capsys)

    assert "claude" not in _commands(decision)
    assert "hand edit" in installed.read_text()
    assert _disk(tmp_path) == before
    assert decision["new_status"] == "promoted"


def test_demotion_removes_route_policy_and_lists_rollback_commands_without_rolling_back(tmp_path, capsys):
    _import(tmp_path, capsys)
    _install_all(tmp_path, capsys)
    seed_registry_skill_scorecard_promotion(tmp_path, SKILL)
    promoted = _reconcile(tmp_path, capsys)
    assert promoted["new_status"] == "promoted"
    assert "route_policy" in _status(tmp_path)
    seed_registry_skill_scorecard_hurt(tmp_path, SKILL)
    before = _disk(tmp_path)

    decision = _reconcile(tmp_path, capsys)

    assert decision["action"] == "rollback"
    assert decision["execution"] == "routing-only"
    assert decision["new_status"] == "demoted"
    assert SKILL in decision["applied"]
    assert _status(tmp_path)["status"] == "demoted"
    assert "route_policy" not in _status(tmp_path)
    assert _disk(tmp_path) == before
    commands = _commands(decision)
    assert {"claude", "codex"} <= set(commands)
    for harness, argv in commands.items():
        assert argv[:6] == ["brigade", "skills", "rollback", SKILL, "--target", harness]
        assert "--discard-local-edits" not in argv
    # Uninstalled harnesses have nothing to roll back.
    assert "cursor" not in commands


def test_demotion_leaves_a_hand_edited_copy_in_place(tmp_path, capsys):
    _import(tmp_path, capsys)
    _install_all(tmp_path, capsys)
    seed_registry_skill_scorecard_promotion(tmp_path, SKILL)
    _reconcile(tmp_path, capsys)
    installed = tmp_path / ".claude" / "skills" / SKILL / "SKILL.md"
    installed.write_text(installed.read_text() + "\nhand edit\n")
    seed_registry_skill_scorecard_hurt(tmp_path, SKILL)
    before = _disk(tmp_path)

    decision = _reconcile(tmp_path, capsys)

    assert decision["new_status"] == "demoted"
    assert "hand edit" in installed.read_text()
    assert _disk(tmp_path) == before


def test_a_forged_receipt_pointing_at_an_attacker_snapshot_changes_nothing_on_disk(tmp_path, capsys):
    _import(tmp_path, capsys)
    assert skills_cmd.install(workspace=tmp_path, skill=f"registry:{SKILL}", harness="claude", json_output=True) == 0
    updated = _write_skill(tmp_path / "src2", body="registry moved on")
    assert skills_cmd.import_skill(target=tmp_path, source=updated, force=True, json_output=True) == 0
    assert skills_cmd.install(workspace=tmp_path, skill=f"registry:{SKILL}", harness="claude", force=True) == 0
    capsys.readouterr()
    # The agent's own user can write the receipt: point its rollback at bytes the agent chose.
    attacker = tmp_path / ".brigade" / "skills" / "rollback" / SKILL / "claude" / "attacker"
    attacker.mkdir(parents=True)
    payload = b"# attacker chose these bytes\n"
    (attacker / "SKILL.md").write_bytes(payload)
    receipt_path = tmp_path / ".brigade" / "skills" / "installs" / f"{SKILL}-claude.json"
    receipt = json.loads(receipt_path.read_text())
    receipt["rollback_snapshot"] = str(attacker)
    receipt["rollback_snapshot_fingerprint"] = registry._files_fingerprint({("SKILL.md",): payload})
    receipt_path.write_text(json.dumps(receipt))
    seed_registry_skill_scorecard_promotion(tmp_path, SKILL)
    assert _reconcile(tmp_path, capsys)["new_status"] == "promoted"
    seed_registry_skill_scorecard_hurt(tmp_path, SKILL)
    before = _disk(tmp_path)

    decision = _reconcile(tmp_path, capsys)

    assert decision["new_status"] == "demoted"
    assert _disk(tmp_path) == before
    assert payload not in (tmp_path / ".claude" / "skills" / SKILL / "SKILL.md").read_bytes()
    assert receipt_path.read_bytes() == json.dumps(receipt).encode()


def test_text_output_lists_each_pending_operator_command(tmp_path, capsys):
    _import(tmp_path, capsys)
    seed_registry_skill_scorecard_promotion(tmp_path, SKILL)

    assert outcome_cmd.reconcile(target=tmp_path, apply=True, config=CFG) == 0

    out = capsys.readouterr().out
    assert f"{SKILL} candidate -> promoted" in out
    assert "-> routing-only" in out
    assert f"    operator: brigade skills install {SKILL} --target claude --force" in out


def test_text_output_warns_that_rollback_inputs_are_unauthenticated_and_names_the_inspection(tmp_path, capsys):
    _import(tmp_path, capsys)
    _install_all(tmp_path, capsys)
    seed_registry_skill_scorecard_promotion(tmp_path, SKILL)
    _reconcile(tmp_path, capsys)
    seed_registry_skill_scorecard_hurt(tmp_path, SKILL)
    capsys.readouterr()

    assert outcome_cmd.reconcile(target=tmp_path, apply=True, config=CFG) == 0

    out = capsys.readouterr().out
    assert "not authenticated" in out
    # shlex quoting of the workspace path differs by platform (Windows backslashes are quoted).
    expected = outcome_skill_exec.rollback_inspect_command(tmp_path, SKILL, "claude")
    assert expected.startswith(f"brigade skills history {SKILL} --target ")
    assert f"    inspect first: {expected}" in out
    assert f"    operator: brigade skills rollback {SKILL} --target claude" in out
    assert out.index("inspect first:") < out.index("operator: brigade skills rollback")


def test_the_ratchet_report_counts_pending_physical_actions(tmp_path, capsys):
    _import(tmp_path, capsys)
    seed_registry_skill_scorecard_promotion(tmp_path, SKILL)

    decision = _reconcile(tmp_path, capsys, report=True)

    report = outcome_ratchet_report.load_report(tmp_path)
    assert report["summary"]["pending_physical_actions"] == len(decision["pending_commands"]) > 0
    assert report["pending_physical"][0]["artifact_id"] == SKILL
    assert outcome_ratchet_report.brief_payload(report)["pending_physical_actions"] == len(decision["pending_commands"])
    # Once the operator has installed every copy the count drops to zero on the next report.
    for harness in _commands(decision):
        assert skills_cmd.install(workspace=tmp_path, skill=SKILL, harness=harness, force=True, json_output=True) == 0
    capsys.readouterr()
    assert outcome_cmd.reconcile(target=tmp_path, apply=False, report=True, json_output=True) == 0
    capsys.readouterr()
    assert outcome_ratchet_report.load_report(tmp_path)["summary"]["pending_physical_actions"] == 0
