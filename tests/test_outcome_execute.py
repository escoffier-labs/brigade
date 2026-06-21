import contextlib
import io
import json

from brigade import outcome, outcome_cmd, skills_cmd

from tests.test_skills_cmd import _write_skill


def _import_skill(tmp_path, name="security-review"):
    source = _write_skill(tmp_path / "source", name)
    with contextlib.redirect_stdout(io.StringIO()):
        assert skills_cmd.import_skill(target=tmp_path, source=source, json_output=True) == 0
    return name


def _helped(artifact_id, n):
    return [
        outcome.OutcomeRecord(artifact_id, "skill", f"t{i}", "verify", 1, f"r{i}", f"2026-06-20T0{i}:00:00+00:00")
        for i in range(n)
    ]


def test_reconcile_apply_physically_installs_a_verified_skill(tmp_path, capsys):
    name = _import_skill(tmp_path)
    outcome_cmd.append_records(tmp_path, _helped(name, 2))
    assert outcome_cmd.reconcile(target=tmp_path, apply=True, json_output=True) == 0
    payload = json.loads(capsys.readouterr().out)
    decision = {d["artifact_id"]: d for d in payload["decisions"]}[name]
    assert decision["action"] == "install"
    assert decision["execution"].startswith("installed")
    assert skills_cmd._install_dir(tmp_path.resolve(), "claude", name).exists()


def test_reconcile_rollback_of_a_first_install_uninstalls(tmp_path, capsys):
    cfg = outcome.ReconcileConfig(cooldown_seconds=0)
    name = _import_skill(tmp_path)
    outcome_cmd.append_records(tmp_path, _helped(name, 2))
    assert outcome_cmd.reconcile(target=tmp_path, apply=True, config=cfg, json_output=True) == 0
    capsys.readouterr()
    dest = skills_cmd._install_dir(tmp_path.resolve(), "claude", name)
    assert dest.exists()

    outcome_cmd.append_records(
        tmp_path, [outcome.OutcomeRecord(name, "skill", "t9", "verify", -1, "regress", "2026-06-20T09:00:00+00:00")]
    )
    assert outcome_cmd.reconcile(target=tmp_path, apply=True, config=cfg, json_output=True) == 0
    payload = json.loads(capsys.readouterr().out)
    decision = {d["artifact_id"]: d for d in payload["decisions"]}[name]
    assert decision["action"] == "rollback"
    # first install had no prior snapshot, so the regression uninstalls it cleanly
    assert not dest.exists()


def test_reconcile_dry_run_does_not_install(tmp_path, capsys):
    name = _import_skill(tmp_path)
    outcome_cmd.append_records(tmp_path, _helped(name, 2))
    assert outcome_cmd.reconcile(target=tmp_path, apply=False, json_output=True) == 0
    payload = json.loads(capsys.readouterr().out)
    decision = {d["artifact_id"]: d for d in payload["decisions"]}[name]
    assert decision["execution"] == "dry-run"
    assert not skills_cmd._install_dir(tmp_path.resolve(), "claude", name).exists()


def test_reconcile_card_decision_skips_physical_execution(tmp_path, capsys):
    cfg = outcome.ReconcileConfig(cooldown_seconds=0)
    outcome_cmd.append_records(
        tmp_path,
        [
            outcome.OutcomeRecord("card-x", "card", "t0", "verify", 1, "r0", "2026-06-20T00:00:00+00:00"),
            outcome.OutcomeRecord("card-x", "card", "t1", "verify", 1, "r1", "2026-06-20T01:00:00+00:00"),
        ],
    )
    assert outcome_cmd.reconcile(target=tmp_path, apply=True, config=cfg, json_output=True) == 0
    payload = json.loads(capsys.readouterr().out)
    decision = {d["artifact_id"]: d for d in payload["decisions"]}["card-x"]
    assert decision["action"] == "install"
    assert "v1.1" in decision["execution"]
