import json
import subprocess
from pathlib import Path

from brigade import center_cmd
from brigade import cli
from brigade import handoff_cmd
from brigade import release_cmd
from brigade import repos_cmd
from brigade import security_cmd
from brigade import work_cmd


def _write_json(path: Path, payload: dict):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n")


def _init_repo(path: Path):
    path.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init"], cwd=path, check=True, stdout=subprocess.DEVNULL)
    subprocess.run(["git", "config", "user.email", "dev@example.invalid"], cwd=path, check=True)
    subprocess.run(["git", "config", "user.name", "Dev"], cwd=path, check=True)
    (path / "AGENTS.md").write_text("private guidance that must not be copied\n")
    (path / ".claude" / "memory-handoffs").mkdir(parents=True)
    (path / "README.md").write_text("readme\n")
    (path / "CHANGELOG.md").write_text("changelog\n")
    (path / "ROADMAP.md").write_text("roadmap\n")
    (path / "tests").mkdir()
    subprocess.run(["git", "add", "."], cwd=path, check=True)
    subprocess.run(["git", "commit", "-m", "initial"], cwd=path, check=True, stdout=subprocess.DEVNULL)


def _seed_workspace(path: Path, repos: list[tuple[str, str, Path]]):
    lines: list[str] = []
    for repo_id, label, repo in repos:
        lines.extend(
            [
                "[[repo]]",
                f'id = "{repo_id}"',
                f'label = "{label}"',
                f'path = "{repo.relative_to(path)}"',
                "enabled = true",
                "expect_brigade = true",
                "",
            ]
        )
    config = path / ".brigade" / "repos.toml"
    config.parent.mkdir(parents=True, exist_ok=True)
    config.write_text("\n".join(lines))


def _seed_operator_report(repo: Path):
    _write_json(
        repo / ".brigade" / "center" / "reports" / "operator-one" / "CENTER_EVIDENCE.json",
        {
            "report_id": "operator-one",
            "status": "ready",
            "created_at": "2026-05-30T01:00:00+00:00",
            "report_fingerprint": "fp-operator",
        },
    )


def _seed_release_ready(repo: Path, *, candidate: bool = True):
    _write_json(
        repo / ".brigade" / "work" / "verify-runs" / "verify-one" / "receipt.json",
        {
            "run_id": "verify-one",
            "status": "completed",
            "started_at": "2026-05-30T01:10:00+00:00",
            "source_fingerprint": "fp-verify",
        },
    )
    _write_json(
        repo / ".brigade" / "work" / "closeouts" / "closeout-one" / "closeout.json",
        {
            "closeout_id": "closeout-one",
            "status": "ready",
            "created_at": "2026-05-30T01:20:00+00:00",
            "source_fingerprint": "fp-closeout",
        },
    )
    _write_json(
        repo / ".brigade" / "release" / "runs" / "release-one" / "receipt.json",
        {
            "run_id": "release-one",
            "status": "ready",
            "ready": True,
            "started_at": "2026-05-30T01:30:00+00:00",
            "source_fingerprint": "fp-release",
        },
    )
    if candidate:
        _write_json(
            repo / ".brigade" / "release" / "candidates" / "candidate-one" / "EVIDENCE.json",
            {
                "candidate_id": "candidate-one",
                "status": "ready",
                "ready": True,
                "created_at": "2026-05-30T01:40:00+00:00",
                "source_fingerprint": "fp-candidate",
            },
        )


def _patch_quiet(monkeypatch):
    monkeypatch.setattr(
        security_cmd,
        "health",
        lambda target: {
            "config_path": str(target / ".brigade" / "security.toml"),
            "valid": True,
            "issue_count": 0,
            "top_issue": None,
            "top_finding": None,
            "checks": [],
            "evidence": {"ready": True},
        },
    )
    monkeypatch.setattr(
        handoff_cmd,
        "draft_queue_payload",
        lambda target, **kwargs: {
            "counts": {"pending": 0},
            "issue_count": 0,
            "top_issue": None,
            "latest_ingest_run": None,
            "drafts": [],
            "checks": [],
        },
    )
    monkeypatch.setattr(
        release_cmd,
        "_run_content_guard_check",
        lambda *args, **kwargs: {"name": "content_guard_tip", "status": "ok", "detail": "clean"},
    )
    monkeypatch.setattr(release_cmd, "_content_guard_available", lambda target: True)


def _build_train(tmp_path: Path, monkeypatch, capsys) -> dict:
    ready = tmp_path / "repo-ready"
    blocked = tmp_path / "private" / "actual-repo-name"
    _init_repo(ready)
    _init_repo(blocked)
    _seed_workspace(tmp_path, [("ready", "service ready", ready), ("blocked", "service blocked", blocked)])
    for repo in (ready, blocked):
        _seed_operator_report(repo)
        _seed_release_ready(repo)
    (blocked / "README.md").write_text("dirty\n")
    _patch_quiet(monkeypatch)
    assert repos_cmd.release_build(target=tmp_path, json_output=True) == 0
    train = json.loads(capsys.readouterr().out)
    assert train["status"] == "blocked"
    return train


def test_release_train_actions_build_lifecycle_dedupe_archive_and_privacy(tmp_path, monkeypatch, capsys):
    train = _build_train(tmp_path, monkeypatch, capsys)

    assert repos_cmd.release_actions_build(target=tmp_path, train_id=train["train_id"], json_output=True) == 2
    assert "closed out" in capsys.readouterr().err
    assert (
        repos_cmd.release_closeout(
            target=tmp_path, train_id=train["train_id"], status="reviewed", reason="reviewed", json_output=True
        )
        == 0
    )
    capsys.readouterr()

    assert (
        cli.main(["repos", "release", "actions", "plan", train["train_id"], "--target", str(tmp_path), "--json"]) == 0
    )
    plan = json.loads(capsys.readouterr().out)
    assert plan["action_count"] == 1
    assert plan["actions"][0]["classification"] == "blocked"
    assert "actual-repo-name" not in json.dumps(plan)
    assert "private guidance that must not be copied" not in json.dumps(plan)

    assert repos_cmd.release_actions_build(target=tmp_path, train_id=train["train_id"], json_output=True) == 0
    built = json.loads(capsys.readouterr().out)
    assert built["created_count"] == 1
    action_id = built["created_actions"][0]["release_action_id"]
    assert repos_cmd.release_actions_build(target=tmp_path, train_id=train["train_id"], json_output=True) == 0
    deduped = json.loads(capsys.readouterr().out)
    assert deduped["skipped_count"] == 1

    assert repos_cmd.release_actions_show(target=tmp_path, action_id=action_id, json_output=True) == 0
    assert json.loads(capsys.readouterr().out)["action"]["repo_id"] == "blocked"
    assert repos_cmd.release_actions_start(target=tmp_path, action_id=action_id, json_output=True) == 0
    assert json.loads(capsys.readouterr().out)["action"]["status"] == "active"
    assert repos_cmd.release_actions_done(target=tmp_path, action_id=action_id, json_output=True) == 0
    assert json.loads(capsys.readouterr().out)["action"]["status"] == "done"
    assert repos_cmd.release_actions_archive_completed(target=tmp_path, json_output=True) == 0
    archived = json.loads(capsys.readouterr().out)
    assert archived["archived_count"] == 1
    assert repos_cmd.release_actions_list(target=tmp_path, json_output=True) == 0
    assert json.loads(capsys.readouterr().out)["action_count"] == 0


def test_release_evidence_plan_record_list_show_and_health(tmp_path, monkeypatch, capsys):
    train = _build_train(tmp_path, monkeypatch, capsys)

    assert repos_cmd.release_evidence_plan(target=tmp_path, train_id=train["train_id"], json_output=True) == 0
    plan = json.loads(capsys.readouterr().out)
    assert plan["planned_count"] == 12
    assert {item["status"] for item in plan["planned"]} == {"missing"}

    assert (
        cli.main(
            [
                "repos",
                "release",
                "evidence",
                "record",
                train["train_id"],
                "--repo",
                "blocked",
                "--step",
                "candidate-compare",
                "--status",
                "blocked",
                "--summary",
                "candidate compare found a blocker",
                "--target",
                str(tmp_path),
                "--json",
            ]
        )
        == 0
    )
    recorded = json.loads(capsys.readouterr().out)
    evidence_id = recorded["record"]["evidence_id"]
    assert recorded["created"] is True
    assert (
        repos_cmd.release_evidence_record(
            target=tmp_path,
            train_id=train["train_id"],
            repo_id="blocked",
            step="candidate-compare",
            status="completed",
            summary="candidate compare completed",
            json_output=True,
        )
        == 0
    )
    updated = json.loads(capsys.readouterr().out)
    assert updated["updated"] is True
    assert updated["record"]["evidence_id"] == evidence_id

    assert repos_cmd.release_evidence_list(target=tmp_path, train_id=train["train_id"], json_output=True) == 0
    listed = json.loads(capsys.readouterr().out)
    assert listed["record_count"] == 1
    assert repos_cmd.release_evidence_show(target=tmp_path, evidence_id=evidence_id, json_output=True) == 0
    assert json.loads(capsys.readouterr().out)["record"]["status"] == "completed"

    assert (
        repos_cmd.release_evidence_record(
            target=tmp_path,
            train_id=train["train_id"],
            repo_id="blocked",
            step="release-doctor",
            status="blocked",
            summary="doctor blocked",
            json_output=True,
        )
        == 0
    )
    capsys.readouterr()
    health = repos_cmd.release_train_health(tmp_path)
    assert health["evidence"]["blocked_count"] == 1
    assert any(check["name"] == "repo_fleet_release_evidence_blocked" for check in health["checks"])


def test_release_matrix_writes_markdown_json_and_surfaces_health(tmp_path, monkeypatch, capsys):
    train = _build_train(tmp_path, monkeypatch, capsys)
    health = repos_cmd.release_train_health(tmp_path)
    assert any(check["name"] == "repo_fleet_release_matrix_missing" for check in health["checks"])
    assert (
        repos_cmd.release_evidence_record(
            target=tmp_path,
            train_id=train["train_id"],
            repo_id="blocked",
            step="release-doctor",
            status="blocked",
            summary="doctor blocked",
            json_output=True,
        )
        == 0
    )
    capsys.readouterr()
    assert (
        repos_cmd.release_waiver_record(
            target=tmp_path,
            train_id=train["train_id"],
            scope="blocked-evidence",
            repo_id="blocked",
            reason="reviewed blocker",
            expires_at="2026-06-30T00:00:00+00:00",
            json_output=True,
        )
        == 0
    )
    waiver = json.loads(capsys.readouterr().out)
    assert waiver["waiver"]["scope"] == "blocked-evidence"

    assert cli.main(["repos", "release", "matrix", train["train_id"], "--target", str(tmp_path), "--json"]) == 0
    matrix = json.loads(capsys.readouterr().out)
    report = matrix["report"]
    rows = {row["repo_id"]: row for row in report["matrix"]["rows"]}
    assert rows["ready"]["evidence_status"] == "missing-evidence"
    assert "blocked-evidence" in rows["blocked"]["waived_scopes"]
    assert rows["blocked"]["evidence_steps"][1]["step"] == "release-doctor"
    assert rows["blocked"]["evidence_steps"][1]["status"] == "blocked"
    assert report["matrix"]["waiver_count"] == 1
    train_path = tmp_path / ".brigade" / "repos" / "releases" / train["train_id"]
    assert (train_path / "RELEASE_TRAIN_MATRIX.json").is_file()
    assert (train_path / "RELEASE_TRAIN_MATRIX.md").is_file()
    assert "actual-repo-name" not in (train_path / "RELEASE_TRAIN_MATRIX.json").read_text()
    assert "private guidance that must not be copied" not in (train_path / "RELEASE_TRAIN_MATRIX.md").read_text()
    assert not any(
        check["name"] == "repo_fleet_release_matrix_missing"
        for check in repos_cmd.release_train_health(tmp_path)["checks"]
    )


def test_release_train_actions_and_evidence_integrate_with_daily_surfaces(tmp_path, monkeypatch, capsys):
    _init_repo(tmp_path)
    _seed_release_ready(tmp_path)
    train = _build_train(tmp_path, monkeypatch, capsys)
    assert (
        repos_cmd.release_closeout(target=tmp_path, train_id=train["train_id"], status="reviewed", json_output=True)
        == 0
    )
    capsys.readouterr()
    assert repos_cmd.release_actions_build(target=tmp_path, train_id=train["train_id"], json_output=True) == 0
    capsys.readouterr()
    assert (
        repos_cmd.release_evidence_record(
            target=tmp_path,
            train_id=train["train_id"],
            repo_id="blocked",
            step="release-doctor",
            status="blocked",
            json_output=True,
        )
        == 0
    )
    capsys.readouterr()

    health = repos_cmd.health(tmp_path)
    names = {check["name"] for check in health["release_train"]["checks"]}
    assert "repo_fleet_release_actions_open" in names
    assert "repo_fleet_release_evidence_blocked" in names
    assert repos_cmd.doctor(target=tmp_path) == 1
    doctor_out = capsys.readouterr().out
    assert "repo_fleet_release_actions_open" in doctor_out
    assert "repo_fleet_release_evidence_blocked" in doctor_out

    assert center_cmd.status(target=tmp_path, json_output=True) == 0
    center = json.loads(capsys.readouterr().out)
    assert center["repo_fleet"]["release_train"]["actions"]["open_count"] == 1
    assert center_cmd.reviews(target=tmp_path, json_output=True) == 0
    reviews = json.loads(capsys.readouterr().out)
    assert any(
        item["subsystem"] == "repo-fleet" and item["local_id"] == "repo_fleet_release_train_blocked"
        for item in reviews["reviews"]
    )

    assert work_cmd.brief(target=tmp_path, json_output=True) == 0
    brief = json.loads(capsys.readouterr().out)
    assert brief["repo_fleet"]["release_train"]["actions"]["open_count"] == 1
    assert release_cmd.doctor(target=tmp_path, base_ref=None, json_output=True) == 1
    release = json.loads(capsys.readouterr().out)
    assert any("repo fleet release train" in warning for warning in release["warnings"])
