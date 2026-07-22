from __future__ import annotations

import json
import os
from datetime import timedelta
from pathlib import Path

from brigade import cli, cursor_user_cmd, localio, repos_cmd
from brigade.claude_hooks import runtime
from brigade.install import install_selection
from brigade.receipts_cmd import _receipt_hash
from brigade.selection import Selection


def _fleet_config(workspace: Path, repos: list[tuple[str, Path]]) -> None:
    config = workspace / ".brigade" / "repos.toml"
    config.parent.mkdir(parents=True, exist_ok=True)
    config.write_text(
        "".join(
            (
                "[[repo]]\n"
                f'id = "{repo_id}"\n'
                f'label = "{repo_id} repo"\n'
                f'path = "{repo.relative_to(workspace)}"\n'
                "enabled = true\n"
                "expect_brigade = true\n\n"
            )
            for repo_id, repo in repos
        )
    )


def _wired_repo(workspace: Path, repo_id: str, harness: str) -> Path:
    repo = workspace / "repos" / repo_id
    assert (
        install_selection(
            repo,
            Selection(depth="repo", harnesses=[harness], owner=harness, includes=[]),
        )
        == 0
    )
    return repo


def _write_claude_session(
    repo: Path,
    session_id: str,
    *,
    started_at,
    last_write_at,
    last_verification_write_at=None,
) -> str:
    fingerprint = runtime._session_fingerprint(session_id)
    state = {
        "session_id": session_id,
        "session_fingerprint": fingerprint,
        "target": str(repo.resolve()),
        "started_at": started_at.isoformat(),
        "last_write_at": last_write_at.isoformat(),
        "briefed": True,
        "write_observed": True,
        "verify_denied_count": 0,
    }
    if last_verification_write_at is not None:
        state["last_verification_write_at"] = last_verification_write_at.isoformat()
    runtime.write_session_state(
        repo,
        session_id,
        state,
    )
    return fingerprint


def _write_compliant_evidence(repo: Path, fingerprint: str, *, started_at) -> str:
    run_id = "verify-active"
    run_dir = repo / ".brigade" / "work" / "verify-runs" / run_id
    run_dir.mkdir(parents=True)
    receipt_path = run_dir / "receipt.json"
    receipt = {
        "run_id": run_id,
        "path": str(run_dir),
        "status": "completed",
        "started_at": started_at.isoformat(),
        "completed_at": (started_at + timedelta(minutes=1)).isoformat(),
        "harness_session": {"harness": "claude", "fingerprint": fingerprint},
        "commands": [{"status": "completed", "exit_code": 0}],
        "code_graph_delta": {"status": "ok", "ok": True},
    }
    receipt_path.write_text(json.dumps(receipt) + "\n")
    records = repo / "memory" / "outcome" / "records.jsonl"
    records.parent.mkdir(parents=True)
    records.write_text(json.dumps({"source": "verify", "evidence_ref": str(receipt_path)}) + "\n")
    receipt_hash, _ = _receipt_hash(receipt, receipt_path)
    cursor = repo / ".brigade" / "work" / "miseledger-export-cursor.json"
    cursor.write_text(
        json.dumps(
            {
                "schema": "brigade.miseledger_export_cursor.v1",
                "source": "brigade",
                "raw_hashes": [receipt_hash],
            }
        )
        + "\n"
    )
    inbox = repo / ".claude" / "memory-handoffs"
    handoff = inbox / "2026-07-17-0400-adoption.md"
    handoff.write_text("durable fleet finding\n")
    timestamp = (started_at + timedelta(minutes=2)).timestamp()
    os.utime(handoff, (timestamp, timestamp))
    return run_id


def test_adoption_report_distinguishes_all_fleet_states(tmp_path, monkeypatch, capsys):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    now = localio.utc_now()

    active = _wired_repo(workspace, "active", "claude")
    fingerprint = _write_claude_session(
        active,
        "active-session",
        started_at=now - timedelta(hours=2),
        last_write_at=now - timedelta(hours=1),
    )
    _write_compliant_evidence(active, fingerprint, started_at=now - timedelta(minutes=30))

    bypassed = _wired_repo(workspace, "bypassed", "claude")
    _write_claude_session(
        bypassed,
        "bypassed-session",
        started_at=now - timedelta(hours=2),
        last_write_at=now - timedelta(hours=1),
    )

    idle = _wired_repo(workspace, "idle", "claude")

    advisory = _wired_repo(workspace, "advisory", "claude")
    from brigade.claude_hooks.install_cmd import hooks_uninstall

    assert hooks_uninstall(target=advisory) == 0

    stale = _wired_repo(workspace, "stale", "claude")
    (stale / ".claude" / "skills" / "brigade-work" / "SKILL.md").write_text("stale\n")

    partial = _wired_repo(workspace, "partial", "claude")
    (partial / ".claude" / "skills" / "brigade-work" / "SKILL.md").unlink()

    unwired = workspace / "repos" / "unwired"
    unwired.mkdir(parents=True)

    cursor = _wired_repo(workspace, "cursor", "cursor")
    monkeypatch.setattr(cursor_user_cmd, "_home_dir", lambda: tmp_path / "home")
    assert cursor_user_cmd.install(write=True, json_output=True) == 0
    capsys.readouterr()

<<<<<<< HEAD
=======
    # Cursor readiness requires schema-v2 generated ownership and a separately
    # registry-projected brigade-work package.
    from brigade import harness_profile_cmd

    cursor_root = cursor_user_cmd._cursor_root()
    v2_state = harness_profile_cmd.empty_profile_state(workspace=workspace, harness="cursor")
    generated = cursor_user_cmd.cursor_generated_files(cursor_root)
    v2_state["generated"] = {
        "files": {
            cursor_user_cmd._relative(cursor_root, path): cursor_user_cmd._digest_text(text)
            for path, (text, _exec, _surface) in generated.items()
        },
        "hooks": {},
        "created_directories": [],
    }
    harness_profile_cmd.write_profile_state(state_path=cursor_root / "brigade" / "install-state.json", state=v2_state)
    skill_source = Path(cursor_user_cmd.__file__).parent / "templates" / "skills" / "brigade-work"
    skill_root = cursor_root / "skills" / "brigade-work"
    skill_root.mkdir(parents=True, exist_ok=True)
    for name in ("SKILL.md", "skill.json", "CHANGELOG.md"):
        (skill_root / name).write_bytes((skill_source / name).read_bytes())

>>>>>>> 7d7fab5 (feat(harness): add aggregate user profile CLI)
    repos = [
        ("active", active),
        ("bypassed", bypassed),
        ("idle", idle),
        ("advisory", advisory),
        ("stale", stale),
        ("partial", partial),
        ("unwired", unwired),
        ("cursor", cursor),
    ]
    _fleet_config(workspace, repos)

    assert repos_cmd.adoption_report(target=workspace, harnesses=["claude", "cursor"], days=7, json_output=True) == 0
    payload = json.loads(capsys.readouterr().out)
    states = {(row["repo_id"], row["harness"]): row["state"] for row in payload["rows"]}

    assert states[("active", "claude")] == "active"
    assert states[("bypassed", "claude")] == "bypassed"
    assert states[("idle", "claude")] == "enforced-idle"
    assert states[("advisory", "claude")] == "advisory-only"
    assert states[("stale", "claude")] == "stale"
    assert states[("partial", "claude")] == "partial"
    assert states[("unwired", "claude")] == "unwired"
    assert states[("cursor", "cursor")] == "advisory-only"
    assert payload["denominators"] == {
        "active_sessions": 2,
        "active_repositories": 2,
        "wired_repositories": 7,
        "compliant_sessions": 1,
        "bypassed_sessions": 1,
    }
    assert payload["monitor"]["alert_states"] == ["bypassed", "stale", "unwired"]
    assert {item["row_key"] for item in payload["monitor"]["alerts"]} == {
        "bypassed:claude",
        "stale:claude",
        "unwired:claude",
        "unwired:cursor",
    }
    assert all(row["next_command"] for row in payload["rows"] if row["state"] != "active")


def test_adoption_counts_failed_or_rejected_verify_only_when_captured(tmp_path, capsys):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    repo = _wired_repo(workspace, "service", "claude")
    capsys.readouterr()
    _fleet_config(workspace, [("service", repo)])
    now = localio.utc_now()
    fingerprint = _write_claude_session(
        repo,
        "failed-session",
        started_at=now - timedelta(hours=2),
        last_write_at=now - timedelta(hours=1),
    )
    run_dir = repo / ".brigade" / "work" / "verify-runs" / "failed"
    run_dir.mkdir(parents=True)
    receipt_path = run_dir / "receipt.json"
    receipt_path.write_text(
        json.dumps(
            {
                "run_id": "failed",
                "path": str(run_dir),
                "status": "failed",
                "started_at": (now - timedelta(minutes=30)).isoformat(),
                "harness_session": {"harness": "claude", "fingerprint": fingerprint},
                "code_graph_delta": {"status": "ok", "ok": True},
            }
        )
        + "\n"
    )

    assert repos_cmd.adoption_report(target=workspace, harnesses=["claude"], days=7, json_output=True) == 0
    uncaptured = json.loads(capsys.readouterr().out)
    assert uncaptured["rows"][0]["state"] == "bypassed"
    assert uncaptured["rows"][0]["use"]["captured_verification_count"] == 0

    records = repo / "memory" / "outcome" / "records.jsonl"
    records.parent.mkdir(parents=True)
    records.write_text(json.dumps({"source": "verify", "evidence_ref": str(receipt_path)}) + "\n")
    assert repos_cmd.adoption_report(target=workspace, harnesses=["claude"], days=7, json_output=True) == 0
    captured = json.loads(capsys.readouterr().out)
    assert captured["rows"][0]["use"]["captured_verification_count"] == 1
    assert captured["rows"][0]["use"]["failed_or_rejected_captured_count"] == 1


def test_adoption_accepts_verification_before_final_handoff_write(tmp_path, capsys):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    repo = _wired_repo(workspace, "service", "claude")
    capsys.readouterr()
    _fleet_config(workspace, [("service", repo)])
    now = localio.utc_now()
    fingerprint = _write_claude_session(
        repo,
        "handoff-last",
        started_at=now - timedelta(hours=2),
        last_verification_write_at=now - timedelta(hours=1),
        last_write_at=now - timedelta(minutes=20),
    )
    _write_compliant_evidence(repo, fingerprint, started_at=now - timedelta(minutes=30))

    assert repos_cmd.adoption_report(target=workspace, harnesses=["claude"], days=7, json_output=True) == 0
    payload = json.loads(capsys.readouterr().out)

    assert payload["rows"][0]["state"] == "active"
    assert payload["rows"][0]["use"]["compliant_session_count"] == 1


def test_adoption_explains_evidence_split_across_receipts(tmp_path, capsys):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    repo = _wired_repo(workspace, "service", "claude")
    capsys.readouterr()
    _fleet_config(workspace, [("service", repo)])
    now = localio.utc_now()
    fingerprint = _write_claude_session(
        repo,
        "split-evidence",
        started_at=now - timedelta(hours=2),
        last_write_at=now - timedelta(hours=1),
    )
    _write_compliant_evidence(repo, fingerprint, started_at=now - timedelta(minutes=30))
    second_dir = repo / ".brigade" / "work" / "verify-runs" / "verify-exported"
    second_dir.mkdir()
    second_path = second_dir / "receipt.json"
    second = {
        "run_id": "verify-exported",
        "path": str(second_dir),
        "status": "completed",
        "started_at": (now - timedelta(minutes=20)).isoformat(),
        "harness_session": {"harness": "claude", "fingerprint": fingerprint},
        "commands": [{"status": "completed", "exit_code": 0}],
        "code_graph_delta": {"status": "unavailable", "ok": False},
    }
    second_path.write_text(json.dumps(second) + "\n")
    second_hash, _ = _receipt_hash(second, second_path)
    cursor = repo / ".brigade" / "work" / "miseledger-export-cursor.json"
    cursor.write_text(json.dumps({"raw_hashes": [second_hash]}) + "\n")

    assert repos_cmd.adoption_report(target=workspace, harnesses=["claude"], days=7, json_output=True) == 0
    payload = json.loads(capsys.readouterr().out)
    session = payload["rows"][0]["use"]["sessions"][0]

    assert session["status"] == "bypassed"
    assert session["missing"] == ["complete-verification-evidence"]


def test_adoption_scrubs_private_repo_path_from_claude_classifier_issues(tmp_path, capsys):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    repo = _wired_repo(workspace, "service", "claude")
    capsys.readouterr()
    settings = repo / ".claude" / "settings.json"
    settings.unlink()
    settings.mkdir()
    _fleet_config(workspace, [("service", repo)])

    assert repos_cmd.adoption_report(target=workspace, harnesses=["claude"], days=7, json_output=True) == 0
    payload = json.loads(capsys.readouterr().out)

    assert payload["rows"][0]["state"] == "partial"
    assert str(repo) not in json.dumps(payload)
    assert any("service repo/.claude/settings.json" in issue for issue in payload["rows"][0]["issues"])


def test_adoption_repair_is_read_only_and_cli_dispatches_with_options_after_subcommand(tmp_path, capsys):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    repo = workspace / "repos" / "service"
    repo.mkdir(parents=True)
    _fleet_config(workspace, [("service", repo)])

    assert (
        cli.main(
            [
                "repos",
                "adoption",
                "repair",
                "--target",
                str(workspace),
                "--state",
                "unwired",
                "--harness",
                "claude",
                "--json",
            ]
        )
        == 0
    )
    payload = json.loads(capsys.readouterr().out)
    assert payload["dry_run"] is True
    assert payload["would_write"] is False
    assert payload["actions"] == [
        {
            "repo_id": "service",
            "harness": "claude",
            "state": "unwired",
            "command": "brigade operator quickstart --target <repo> --harnesses claude --dry-run",
        }
    ]
    assert not (repo / ".brigade").exists()


def test_adoption_repair_preserves_options_before_subcommand(tmp_path, capsys):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    repo = workspace / "repos" / "service"
    repo.mkdir(parents=True)
    _fleet_config(workspace, [("service", repo)])

    assert (
        cli.main(
            [
                "repos",
                "adoption",
                "--target",
                str(workspace),
                "--harness",
                "claude",
                "--days",
                "30",
                "--json",
                "repair",
                "--state",
                "unwired",
            ]
        )
        == 0
    )
    payload = json.loads(capsys.readouterr().out)

    assert payload["window_days"] == 30
    assert payload["actions"][0]["repo_id"] == "service"


def test_adoption_rejects_invalid_days_and_harness(tmp_path, capsys):
    assert repos_cmd.adoption_report(target=tmp_path, harnesses=["unknown"], days=7, json_output=True) == 2
    assert "supported harness" in capsys.readouterr().err
    assert repos_cmd.adoption_report(target=tmp_path, harnesses=["claude"], days=0, json_output=True) == 2
    assert "positive" in capsys.readouterr().err
