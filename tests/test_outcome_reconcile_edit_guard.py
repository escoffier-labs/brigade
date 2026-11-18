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
from datetime import datetime, timedelta, timezone
from pathlib import Path

from brigade import cli, outcome, outcome_cmd, outcome_ratchet_report, outcome_skill_exec, skills_cmd
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


def _demote(tmp_path: Path, capsys) -> None:
    """Promote then demote the skill through reconcile, leaving both copies installed."""
    _import(tmp_path, capsys)
    _install_all(tmp_path, capsys)
    seed_registry_skill_scorecard_promotion(tmp_path, SKILL)
    _reconcile(tmp_path, capsys)
    seed_registry_skill_scorecard_hurt(tmp_path, SKILL)
    assert _reconcile(tmp_path, capsys)["new_status"] == "demoted"


def _age_status(tmp_path: Path, days: int) -> None:
    """Rewrite the skill's last transition to ``days`` ago, as a demotion from before routing-only would carry."""
    path = outcome_cmd._status_path(tmp_path)
    data = json.loads(path.read_text())
    data["artifacts"][SKILL]["last_action_ts"] = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    path.write_text(json.dumps(data))


def _dry_run_report(tmp_path: Path, capsys, config: outcome.ReconcileConfig = CFG) -> dict:
    assert outcome_cmd.reconcile(target=tmp_path, apply=False, config=config, json_output=True, report=True) == 0
    capsys.readouterr()
    return outcome_ratchet_report.load_report(tmp_path)


def test_an_old_demotion_is_not_listed_but_is_counted_as_stale(tmp_path, capsys):
    _demote(tmp_path, capsys)
    _age_status(tmp_path, 90)

    report = _dry_run_report(tmp_path, capsys)

    assert report["summary"]["pending_physical_actions"] == 0
    assert report["summary"]["stale_physical_actions"] >= 2
    assert report["pending_physical"] == []
    brief = outcome_ratchet_report.brief_payload(report)
    assert brief["pending_physical_actions"] == 0
    assert brief["stale_physical_actions"] == report["summary"]["stale_physical_actions"]


def test_a_skill_with_no_transition_time_is_stale(tmp_path, capsys):
    _demote(tmp_path, capsys)
    path = outcome_cmd._status_path(tmp_path)
    data = json.loads(path.read_text())
    del data["artifacts"][SKILL]["last_action_ts"]
    path.write_text(json.dumps(data))

    report = _dry_run_report(tmp_path, capsys)

    assert report["summary"]["pending_physical_actions"] == 0
    assert report["summary"]["stale_physical_actions"] >= 2


def test_a_fresh_demotion_is_listed_with_its_age_in_json_and_text(tmp_path, capsys):
    _demote(tmp_path, capsys)
    _age_status(tmp_path, 3)

    report = _dry_run_report(tmp_path, capsys)

    assert report["summary"]["pending_physical_actions"] >= 2
    assert report["summary"]["stale_physical_actions"] == 0
    (row,) = report["pending_physical"]
    assert row["artifact_id"] == SKILL
    assert row["status"] == "demoted"
    assert row["status_changed_at"]
    assert all(command.startswith("brigade skills rollback") for command in row["commands"])

    assert cli.main(["outcome", "ratchet", "--target", str(tmp_path)]) == 0
    out = capsys.readouterr().out
    assert f"operator: brigade skills rollback {SKILL} --target claude" in out
    assert "(demoted 3 days ago)" in out
    assert "stale_physical_actions" not in out


def test_the_window_is_a_config_field(tmp_path, capsys):
    _demote(tmp_path, capsys)
    _age_status(tmp_path, 3)

    narrow = _dry_run_report(
        tmp_path, capsys, outcome.ReconcileConfig(cooldown_seconds=0, pending_physical_window_days=2)
    )
    assert narrow["summary"]["pending_physical_actions"] == 0
    assert narrow["summary"]["stale_physical_actions"] >= 2

    wide = _dry_run_report(
        tmp_path, capsys, outcome.ReconcileConfig(cooldown_seconds=0, pending_physical_window_days=30)
    )
    assert wide["summary"]["pending_physical_actions"] >= 2
    assert wide["summary"]["stale_physical_actions"] == 0


def test_a_decision_from_the_current_run_is_always_listed_with_its_time(tmp_path, capsys):
    _import(tmp_path, capsys)
    _install_all(tmp_path, capsys)
    seed_registry_skill_scorecard_promotion(tmp_path, SKILL)
    _reconcile(tmp_path, capsys)
    seed_registry_skill_scorecard_hurt(tmp_path, SKILL)
    # A negative window excludes every status by age, so only the current-run rule can list it.
    config = outcome.ReconcileConfig(cooldown_seconds=0, pending_physical_window_days=-1)

    assert outcome_cmd.reconcile(target=tmp_path, apply=True, config=config, json_output=True, report=True) == 0
    payload = json.loads(capsys.readouterr().out)

    decision = next(item for item in payload["decisions"] if item["artifact_id"] == SKILL)
    assert decision["status_changed_at"]
    report = outcome_ratchet_report.load_report(tmp_path)
    assert report["summary"]["pending_physical_actions"] == len(decision["pending_commands"]) > 0
    assert report["summary"]["stale_physical_actions"] == 0
    assert report["pending_physical"][0]["status_changed_at"] == decision["status_changed_at"]


def test_text_output_labels_a_current_run_command_with_its_age(tmp_path, capsys):
    _import(tmp_path, capsys)
    seed_registry_skill_scorecard_promotion(tmp_path, SKILL)

    assert outcome_cmd.reconcile(target=tmp_path, apply=True, config=CFG) == 0

    operator_lines = [
        line for line in capsys.readouterr().out.splitlines() if "operator: brigade skills install" in line
    ]
    assert operator_lines
    assert all(line.endswith("(promoted today)") for line in operator_lines)


def _set_status_ts(tmp_path: Path, value: object) -> None:
    path = outcome_cmd._status_path(tmp_path)
    data = json.loads(path.read_text())
    data["artifacts"][SKILL]["last_action_ts"] = value
    path.write_text(json.dumps(data))


def test_a_promoted_install_older_than_the_window_stays_listed(tmp_path, capsys):
    # Install commands come from current drift, not history: an install that was never run, or a copy
    # that drifted later, must not stop being reported just because the promotion is old.
    _import(tmp_path, capsys)
    seed_registry_skill_scorecard_promotion(tmp_path, SKILL)
    assert _reconcile(tmp_path, capsys)["new_status"] == "promoted"
    _age_status(tmp_path, 90)

    report = _dry_run_report(tmp_path, capsys)

    assert report["summary"]["pending_physical_actions"] >= 2
    assert report["summary"]["stale_physical_actions"] == 0
    assert report.get("stale_physical") == []
    (row,) = report["pending_physical"]
    assert row["artifact_id"] == SKILL
    assert row["status"] == "promoted"
    assert all(command.startswith("brigade skills install") for command in row["commands"])


def test_ratchet_text_and_json_list_stale_rollbacks_under_their_own_heading(tmp_path, capsys):
    _demote(tmp_path, capsys)
    _age_status(tmp_path, 90)
    _dry_run_report(tmp_path, capsys)

    assert cli.main(["outcome", "ratchet", "--target", str(tmp_path)]) == 0
    out = capsys.readouterr().out
    assert "pending_physical_actions: 0" in out
    assert "stale:" in out
    assert f"brigade skills rollback {SKILL} --target claude" in out
    assert "(demoted 90 days ago)" in out
    # Stale rows are not presented as pending operator actions.
    assert "operator:" not in out

    assert cli.main(["outcome", "ratchet", "--target", str(tmp_path), "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["pending_physical"] == []
    (row,) = payload["stale_physical"]
    assert row["artifact_id"] == SKILL
    assert row["status"] == "demoted"
    assert row["status_changed_at"]
    assert all(command.startswith("brigade skills rollback") for command in row["commands"])


def test_a_malformed_transition_time_is_stale_and_still_listed_under_stale(tmp_path, capsys):
    _demote(tmp_path, capsys)
    _set_status_ts(tmp_path, "not-a-date")

    report = _dry_run_report(tmp_path, capsys)

    assert report["summary"]["pending_physical_actions"] == 0
    assert report["summary"]["stale_physical_actions"] >= 2
    assert report["pending_physical"] == []
    assert [row["artifact_id"] for row in report["stale_physical"]] == [SKILL]
    assert cli.main(["outcome", "ratchet", "--target", str(tmp_path)]) == 0
    assert "unknown age" in capsys.readouterr().out


def _fake_commands(monkeypatch, ids: list[str]) -> None:
    monkeypatch.setattr(
        outcome_skill_exec,
        "pending_commands",
        lambda target, artifact_id, status, plan=None: (
            [f"brigade skills {status} {artifact_id}"] if artifact_id in ids else []
        ),
    )


def _status_map(now: datetime, **entries: tuple[str, object]) -> dict[str, dict]:
    return {name: {"status": status, "last_action_ts": ts} for name, (status, ts) in entries.items()}


def test_a_run_with_one_recent_and_one_stale_demotion_splits_them(tmp_path, monkeypatch):
    now = datetime(2026, 11, 17, tzinfo=timezone.utc)
    status_map = _status_map(
        now,
        recent=("demoted", (now - timedelta(days=3)).isoformat()),
        old=("demoted", (now - timedelta(days=90)).isoformat()),
    )
    _fake_commands(monkeypatch, ["recent", "old"])

    pending = outcome_skill_exec.pending_by_artifact(
        tmp_path, status_map, {}, now=now, window_days=14, current_run_ids=()
    )

    assert set(pending.recent) == {"recent"}
    assert set(pending.stale) == {"old"}
    assert pending.stale_actions == 1
    assert pending.stale["old"].status == "demoted"


def test_the_window_compares_exact_time_not_whole_days(tmp_path, monkeypatch):
    now = datetime(2026, 11, 17, 12, tzinfo=timezone.utc)
    # 14 days and 23 hours old floors to 14 whole days, but it is older than a 14 day window.
    over = (now - timedelta(days=14, hours=23)).isoformat()
    exact = (now - timedelta(days=14)).isoformat()
    status_map = _status_map(now, over=("demoted", over), exact=("demoted", exact))
    _fake_commands(monkeypatch, ["over", "exact"])

    pending = outcome_skill_exec.pending_by_artifact(tmp_path, status_map, {}, now=now, window_days=14)

    assert set(pending.recent) == {"exact"}
    assert set(pending.stale) == {"over"}


def test_a_future_timestamp_cannot_prove_a_demotion_is_recent(tmp_path, monkeypatch):
    now = datetime(2026, 11, 17, 12, tzinfo=timezone.utc)
    future = (now + timedelta(days=30)).isoformat()
    status_map = _status_map(now, future=("demoted", future))
    _fake_commands(monkeypatch, ["future"])

    pending = outcome_skill_exec.pending_by_artifact(tmp_path, status_map, {}, now=now, window_days=14)

    assert set(pending.stale) == {"future"} and pending.recent == {}
    assert outcome_skill_exec.age_label("demoted", future, now) == "demoted, unknown age"


def test_a_promoted_install_is_listed_whatever_its_age_or_timestamp(tmp_path, monkeypatch):
    now = datetime(2026, 11, 17, tzinfo=timezone.utc)
    status_map = _status_map(
        now,
        ancient=("promoted", (now - timedelta(days=400)).isoformat()),
        malformed=("promoted", "garbage"),
        missing=("promoted", None),
    )
    _fake_commands(monkeypatch, ["ancient", "malformed", "missing"])
    monkeypatch.setattr(outcome_skill_exec, "_read_plan", lambda target: [])

    pending = outcome_skill_exec.pending_by_artifact(tmp_path, status_map, {}, now=now, window_days=14)

    assert set(pending.recent) == {"ancient", "malformed", "missing"}
    assert pending.stale == {} and pending.stale_actions == 0


def test_pending_rows_sort_current_run_then_newest_before_the_cap(tmp_path):
    base = datetime(2026, 11, 17, tzinfo=timezone.utc)
    recent = {
        f"skill-{n:02d}": outcome_skill_exec.PendingAction(
            "demoted", (base - timedelta(days=n % 10)).isoformat(), [f"brigade skills rollback skill-{n:02d}"]
        )
        for n in range(25)
    }
    recent["zzz-current"] = outcome_skill_exec.PendingAction(
        "demoted", (base - timedelta(days=13)).isoformat(), ["brigade skills rollback zzz-current"], current_run=True
    )
    stale = {
        f"old-{n:02d}": outcome_skill_exec.PendingAction(
            "demoted", (base - timedelta(days=100 + n)).isoformat(), [f"brigade skills rollback old-{n:02d}"]
        )
        for n in range(25)
    }

    report = outcome_ratchet_report.build_report(
        [],
        apply=True,
        generated_at=base,
        target=tmp_path,
        pending=outcome_skill_exec.PendingPhysical(recent=recent, stale=stale, stale_actions=25),
    )

    listed = [row["artifact_id"] for row in report["pending_physical"]]
    assert len(listed) == 20
    assert listed[0] == "zzz-current"
    assert report["summary"]["pending_physical_actions"] == 26
    # Newest first, then artifact id: skill-00 and skill-10 share the newest day.
    assert listed[1:4] == ["skill-00", "skill-10", "skill-20"]
    stale_listed = [row["artifact_id"] for row in report["stale_physical"]]
    assert stale_listed[0] == "old-00" and len(stale_listed) == 20
    assert report["summary"]["stale_physical_actions"] == 25


def test_a_bump_shows_the_same_pending_install_in_reconcile_output_and_the_report(tmp_path, capsys):
    _import(tmp_path, capsys)
    seed_registry_skill_scorecard_promotion(tmp_path, SKILL)
    assert _reconcile(tmp_path, capsys)["action"] == "install"
    config = outcome.ReconcileConfig(cooldown_seconds=0, bump_min_helped=1)

    assert outcome_cmd.reconcile(target=tmp_path, apply=True, config=config, json_output=True, report=True) == 0
    payload = json.loads(capsys.readouterr().out)
    decision = next(item for item in payload["decisions"] if item["artifact_id"] == SKILL)
    assert decision["action"] == "bump"
    report = outcome_ratchet_report.load_report(tmp_path)
    assert set(decision["pending_commands"]) == {
        command for row in report["pending_physical"] for command in row["commands"]
    }
    assert decision["pending_commands"]

    assert outcome_cmd.reconcile(target=tmp_path, apply=True, config=config, report=True) == 0
    out = capsys.readouterr().out
    for command in decision["pending_commands"]:
        assert f"operator: {command}" in out
