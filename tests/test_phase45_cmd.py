import json

from brigade import cli
from brigade import repos_cmd

from tests.test_phase44_cmd import _build_train


def _reviewed_train(tmp_path, monkeypatch, capsys):
    train = _build_train(tmp_path, monkeypatch, capsys)
    assert (
        repos_cmd.release_closeout(target=tmp_path, train_id=train["train_id"], status="reviewed", json_output=True)
        == 0
    )
    capsys.readouterr()
    assert repos_cmd.release_actions_build(target=tmp_path, train_id=train["train_id"], json_output=True) == 0
    built = json.loads(capsys.readouterr().out)
    return train, built["created_actions"][0]["release_action_id"]


def _record_required_evidence(tmp_path, train_id: str, repo_id: str, *, status: str = "completed"):
    for step in repos_cmd.REQUIRED_RELEASE_EVIDENCE_STEPS:
        assert (
            repos_cmd.release_evidence_record(
                target=tmp_path,
                train_id=train_id,
                repo_id=repo_id,
                step=step,
                status=status,
                summary=f"{step} {status}",
                json_output=True,
            )
            == 0
        )


def test_release_reconcile_marks_actions_done_when_evidence_is_complete(tmp_path, monkeypatch, capsys):
    train, action_id = _reviewed_train(tmp_path, monkeypatch, capsys)

    assert cli.main(["repos", "release", "reconcile", train["train_id"], "--target", str(tmp_path), "--json"]) == 0
    missing = json.loads(capsys.readouterr().out)
    assert missing["results"][0]["resolution_status"] == "missing-evidence"
    assert missing["results"][0]["status"] == "pending"

    _record_required_evidence(tmp_path, train["train_id"], "blocked")
    capsys.readouterr()
    assert repos_cmd.release_reconcile(target=tmp_path, train_id=train["train_id"], json_output=True) == 0
    reconciled = json.loads(capsys.readouterr().out)
    assert reconciled["results"][0]["release_action_id"] == action_id
    assert reconciled["results"][0]["resolution_status"] == "evidence-complete"
    assert reconciled["results"][0]["status"] == "done"

    assert repos_cmd.release_summary(target=tmp_path, train_id=train["train_id"], json_output=True) == 0
    summary = json.loads(capsys.readouterr().out)
    assert summary["unresolved_action_count"] == 0
    blocked_repo = next(repo for repo in summary["repos"] if repo["repo_id"] == "blocked")
    assert blocked_repo["evidence_status"] == "manually-completed"

    assert (
        repos_cmd.release_closeout(
            target=tmp_path, train_id=train["train_id"], status="reviewed", reason="final", json_output=True
        )
        == 0
    )
    closeout = json.loads(capsys.readouterr().out)
    assert closeout["summary"]["unresolved_action_count"] == 0
    assert closeout["summary"]["summary_fingerprint"]


def test_release_reconcile_keeps_actions_open_for_blocked_or_missing_evidence(tmp_path, monkeypatch, capsys):
    train, action_id = _reviewed_train(tmp_path, monkeypatch, capsys)
    assert (
        repos_cmd.release_evidence_record(
            target=tmp_path,
            train_id=train["train_id"],
            repo_id="blocked",
            step="verification",
            status="blocked",
            summary="verification blocked",
            json_output=True,
        )
        == 0
    )
    capsys.readouterr()

    assert repos_cmd.release_reconcile(target=tmp_path, train_id=train["train_id"], json_output=True) == 0
    blocked = json.loads(capsys.readouterr().out)
    assert blocked["results"][0]["release_action_id"] == action_id
    assert blocked["results"][0]["resolution_status"] == "blocked-evidence"
    assert blocked["results"][0]["status"] == "pending"
    assert "verification" in blocked["results"][0]["blocked_evidence_steps"]

    health = repos_cmd.release_train_health(tmp_path)
    names = {check["name"] for check in health["checks"]}
    assert "repo_fleet_release_actions_open" in names
    assert "repo_fleet_release_evidence_blocked" in names
    assert any(
        action["resolution_status"] == "blocked-evidence" for action in repos_cmd._read_release_actions(tmp_path)
    )


def test_release_reconcile_accepts_skipped_or_deferred_manual_evidence(tmp_path, monkeypatch, capsys):
    train, _ = _reviewed_train(tmp_path, monkeypatch, capsys)
    statuses = ["completed", "skipped", "deferred", "completed", "skipped", "deferred"]
    for step, status in zip(repos_cmd.REQUIRED_RELEASE_EVIDENCE_STEPS, statuses, strict=True):
        assert (
            repos_cmd.release_evidence_record(
                target=tmp_path,
                train_id=train["train_id"],
                repo_id="blocked",
                step=step,
                status=status,
                summary=f"{step} {status}",
                json_output=True,
            )
            == 0
        )
    capsys.readouterr()

    assert repos_cmd.release_reconcile(target=tmp_path, train_id=train["train_id"], json_output=True) == 0
    reconciled = json.loads(capsys.readouterr().out)
    assert reconciled["results"][0]["resolution_status"] == "evidence-complete"
    assert reconciled["results"][0]["status"] == "done"
    assert repos_cmd.release_summary(target=tmp_path, train_id=train["train_id"], json_output=True) == 0
    summary = json.loads(capsys.readouterr().out)
    blocked_repo = next(repo for repo in summary["repos"] if repo["repo_id"] == "blocked")
    assert blocked_repo["evidence_status"] == "deferred"
