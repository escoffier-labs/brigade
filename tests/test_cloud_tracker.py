"""Contract tests for the cloud dispatch registry (#890)."""

from __future__ import annotations

import hashlib
import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest

from brigade import cli, claude_cloud, cloud_tracker, codex_cloud, cursor_cloud, fleet_client, grokbot_jobs, jules_cloud
from brigade.cli import run_cloud


NOW = datetime(2026, 8, 12, 20, 0, 0, tzinfo=timezone.utc)


def _iso(dt: datetime) -> str:
    return dt.isoformat().replace("+00:00", "Z")


def _prompt_hash(text: str) -> str:
    return "sha256:" + hashlib.sha256(text.encode()).hexdigest()


def _isolate_hosted_providers(monkeypatch) -> None:
    """Keep observe_providers tests off the host's live Cursor/Codex inventory."""
    for key in ("CURSOR_API_KEY", "CURSOR_CLOUD_API_KEY", "BG_AGENT_API_KEY", "JULES_API_KEY"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setattr(codex_cloud, "list_tasks", lambda **kwargs: [])


def _grokbot_spec() -> dict[str, object]:
    return {
        "label": "Tracker-safe Grok Bot job",
        "role": "implementation-worker",
        "repository": "example/brigade",
        "base_ref": "main",
        "ownership_paths": ["src/brigade/cloud_tracker.py"],
        "instructions": "PRIVATE TRACKER INSTRUCTIONS TOKEN=do-not-display",
        "verification_commands": ["pytest -q tests/test_cloud_tracker.py"],
        "artifact": {"kind": "draft-pr"},
        "timeout_seconds": 900,
    }


def _completed_grokbot_job(tmp_path: Path, *, now: datetime = NOW) -> str:
    job_id = grokbot_jobs.enqueue(tmp_path, _grokbot_spec(), "tracker-safe-job", now=now)["job_id"]
    grokbot_jobs.claim(tmp_path, job_id, "bot-a", "lease-a", 600, now=now)
    grokbot_jobs.transition(tmp_path, job_id, "bot-a", "lease-a", "running", now=now + timedelta(seconds=1))
    grokbot_jobs.transition(
        tmp_path,
        job_id,
        "bot-a",
        "lease-a",
        "completed",
        artifact={
            "kind": "draft-pr",
            "url": "https://github.com/example/brigade/pull/123",
            "branch": "grokbot/tracker-safe-job",
        },
        now=now + timedelta(seconds=2),
    )
    return job_id


def test_register_persists_entry_without_prompt_text(tmp_path: Path):
    entry = cloud_tracker.register(
        tmp_path,
        provider="codex-cloud",
        task_id="task_abc123",
        label="issue-890",
        prompt_hash=_prompt_hash("secret prompt text"),
        session_id="sess-1",
        expected_artifact={"kind": "branch", "pattern": "codex/*"},
        branch=None,
        dispatched_at=_iso(NOW),
    )
    assert entry["provider"] == "codex-cloud"
    assert entry["task_id"] == "task_abc123"
    assert "prompt" not in entry
    assert entry["prompt_hash"].startswith("sha256:")
    stored = cloud_tracker.load_registry(tmp_path)
    assert stored["schema"] == cloud_tracker.REGISTRY_SCHEMA
    assert len(stored["entries"]) == 1
    raw = (tmp_path / ".brigade" / "cloud" / "registry.json").read_text()
    assert "secret prompt text" not in raw


def test_adopt_task_and_orphaned_branch(tmp_path: Path):
    task_entry = cloud_tracker.adopt(
        tmp_path,
        provider="codex-cloud",
        task_id="task_running",
        label="already-running",
        prompt_hash=_prompt_hash("x"),
    )
    assert task_entry["source"] == "adopt-task"
    branch_entry = cloud_tracker.adopt(
        tmp_path,
        provider="cursor-cloud",
        branch="cursor/orphaned-fix",
        label="orphaned-branch",
    )
    assert branch_entry["source"] == "adopt-branch"
    assert branch_entry["branch"] == "cursor/orphaned-fix"
    assert cloud_tracker.load_registry(tmp_path)["entries"]


@pytest.mark.parametrize(
    ("classification", "entry", "provider_tasks", "github"),
    [
        (
            "pending",
            {
                "id": "e-pending",
                "provider": "codex-cloud",
                "task_id": "t1",
                "label": "pending",
                "prompt_hash": "sha256:aa",
                "dispatched_at": _iso(NOW - timedelta(hours=1)),
                "source": "dispatch",
                "expected_artifact": {"kind": "diff"},
            },
            {"t1": {"state": "running"}},
            {"branches": [], "prs": []},
        ),
        (
            "ready-to-land",
            {
                "id": "e-ready",
                "provider": "codex-cloud",
                "task_id": "t2",
                "label": "ready",
                "prompt_hash": "sha256:bb",
                "dispatched_at": _iso(NOW - timedelta(hours=2)),
                "source": "dispatch",
                "expected_artifact": {"kind": "branch", "pattern": "codex/*"},
                "branch": "codex/ready-fix",
            },
            {"t2": {"state": "ready", "ready_at": _iso(NOW - timedelta(hours=1))}},
            {
                "branches": [{"name": "codex/ready-fix"}],
                "prs": [{"head": "codex/ready-fix", "state": "OPEN", "number": 12}],
            },
        ),
        (
            "stale",
            {
                "id": "e-stale",
                "provider": "codex-cloud",
                "task_id": "t3",
                "label": "stale-ready",
                "prompt_hash": "sha256:cc",
                "dispatched_at": _iso(NOW - timedelta(hours=30)),
                "source": "dispatch",
                "expected_artifact": {"kind": "diff"},
            },
            {"t3": {"state": "ready", "ready_at": _iso(NOW - timedelta(hours=24))}},
            {"branches": [], "prs": []},
        ),
        (
            "landed",
            {
                "id": "e-landed",
                "provider": "codex-cloud",
                "task_id": "t4",
                "label": "landed",
                "prompt_hash": "sha256:dd",
                "dispatched_at": _iso(NOW - timedelta(hours=10)),
                "source": "dispatch",
                "expected_artifact": {"kind": "branch", "pattern": "codex/*"},
                "branch": "codex/landed-fix",
            },
            {"t4": {"state": "completed"}},
            {
                "branches": [],
                "prs": [{"head": "codex/landed-fix", "state": "MERGED", "number": 99}],
            },
        ),
        (
            "orphaned",
            {
                "id": "e-orphan-reg",
                "provider": "codex-cloud",
                "task_id": "t5",
                "label": "errored-left-branch",
                "prompt_hash": "sha256:ee",
                "dispatched_at": _iso(NOW - timedelta(hours=5)),
                "source": "dispatch",
                "expected_artifact": {"kind": "branch", "pattern": "codex/*"},
                "branch": "codex/orphan-left",
            },
            {"t5": {"state": "failed"}},
            {"branches": [{"name": "codex/orphan-left"}], "prs": []},
        ),
        (
            "needs-investigation",
            {
                "id": "e-needs",
                "provider": "codex-cloud",
                "task_id": "t6",
                "label": "finished-no-branch",
                "prompt_hash": "sha256:ff",
                "dispatched_at": _iso(NOW - timedelta(hours=3)),
                "source": "dispatch",
                "expected_artifact": {"kind": "branch", "pattern": "codex/*"},
                "branch": "codex/missing",
            },
            {"t6": {"state": "finished"}},
            {"branches": [], "prs": []},
        ),
    ],
)
def test_classify_each_state(tmp_path: Path, classification, entry, provider_tasks, github):
    cloud_tracker.save_registry(
        tmp_path,
        {
            "schema": cloud_tracker.REGISTRY_SCHEMA,
            "version": 1,
            "stale_ready_hours": 6,
            "entries": [entry],
        },
    )
    status = cloud_tracker.status_payload(
        tmp_path,
        now=NOW,
        provider_tasks=provider_tasks,
        github=github,
        cursor_wired=False,
    )
    assert status["schema"] == cloud_tracker.STATUS_SCHEMA
    row = next(r for r in status["entries"] if r["id"] == entry["id"])
    assert row["classification"] == classification
    assert "evidence" in row


def test_orphaned_branch_without_registry_entry(tmp_path: Path):
    cloud_tracker.save_registry(
        tmp_path,
        {
            "schema": cloud_tracker.REGISTRY_SCHEMA,
            "version": 1,
            "stale_ready_hours": 6,
            "entries": [],
        },
    )
    status = cloud_tracker.status_payload(
        tmp_path,
        now=NOW,
        provider_tasks={},
        github={"branches": [{"name": "cursor/ghost-branch"}], "prs": []},
        cursor_wired=False,
    )
    orphans = [e for e in status["entries"] if e["classification"] == "orphaned"]
    assert len(orphans) == 1
    assert orphans[0]["branch"] == "cursor/ghost-branch"
    assert orphans[0]["source"] == "github-branch"


def test_cursor_cloud_is_unwired_never_guessed(tmp_path: Path):
    cloud_tracker.register(
        tmp_path,
        provider="cursor-cloud",
        task_id="cursor-task-1",
        label="cursor-job",
        prompt_hash=_prompt_hash("x"),
        dispatched_at=_iso(NOW),
    )
    status = cloud_tracker.status_payload(
        tmp_path,
        now=NOW,
        provider_tasks={},
        github={"branches": [], "prs": []},
        cursor_wired=False,
    )
    assert status["sources"]["cursor-cloud"]["wired"] is False
    assert status["sources"]["cursor-cloud"]["authority"] == "unwired"
    row = status["entries"][0]
    assert row["provider"] == "cursor-cloud"
    assert row["classification"] == "pending"
    assert row["evidence"]["provider"] == "unwired"


def test_grokbot_queue_projects_safe_rows_and_reconciles_claimed_and_draft_pr(tmp_path: Path):
    queued = grokbot_jobs.enqueue(tmp_path, _grokbot_spec(), "queued-tracker-job", now=NOW)["job_id"]
    queued_status = cloud_tracker.status_payload(
        tmp_path, now=NOW, provider_tasks={}, github={"branches": [], "prs": []}
    )
    queued_row = next(row for row in queued_status["entries"] if row.get("job_id") == queued)
    assert queued_row["classification"] == "pending"
    assert queued_row["state"] == "queued"
    assert "PRIVATE TRACKER INSTRUCTIONS" not in json.dumps(queued_status)

    grokbot_jobs.claim(tmp_path, queued, "bot-a", "lease-a", 600, now=NOW)
    claimed_status = cloud_tracker.status_payload(
        tmp_path, now=NOW, provider_tasks={}, github={"branches": [], "prs": []}
    )
    claimed_row = next(row for row in claimed_status["entries"] if row.get("job_id") == queued)
    assert claimed_row["classification"] == "pending"
    assert claimed_row["state"] == "claimed"
    assert claimed_row["claimed_at"] == _iso(NOW)

    completed = _completed_grokbot_job(tmp_path, now=NOW + timedelta(hours=1))
    github = {
        "branches": [{"name": "grokbot/tracker-safe-job"}],
        "prs": [
            {
                "head": "grokbot/tracker-safe-job",
                "state": "OPEN",
                "url": "https://github.com/example/brigade/pull/123",
                "isDraft": True,
                "headRefOid": "a" * 40,
            }
        ],
    }
    completed_status = cloud_tracker.status_payload(tmp_path, now=NOW + timedelta(hours=1, minutes=1), github=github)
    row = next(row for row in completed_status["entries"] if row.get("job_id") == completed)
    assert row["provider"] == "grokbot-cloud"
    assert row["classification"] == "ready-to-land"
    assert row["artifact_refs"] == {
        "kind": "draft-pr",
        "branch": "grokbot/tracker-safe-job",
        "pr_url": "https://github.com/example/brigade/pull/123",
        "is_draft": True,
        "head_sha": "a" * 40,
    }
    matching_branch_rows = [
        entry
        for entry in completed_status["entries"]
        if entry.get("artifact_refs", {}).get("branch") == "grokbot/tracker-safe-job"
        or entry.get("branch") == "grokbot/tracker-safe-job"
    ]
    assert [entry["id"] for entry in matching_branch_rows] == [f"grokbot:{completed}"]
    assert set(row) <= {
        "id",
        "provider",
        "job_id",
        "label",
        "task_hash",
        "state",
        "classification",
        "artifact_refs",
        "source",
        "created_at",
        "updated_at",
        "queued_at",
        "claimed_at",
    }
    assert completed_status["sources"]["grokbot-cloud"] == {"wired": True, "authority": "local-queue"}


def test_grokbot_merged_draft_pr_lands_without_queue_attention(tmp_path: Path):
    completed = _completed_grokbot_job(tmp_path, now=NOW - timedelta(hours=30))
    github = {
        "branches": [],
        "prs": [
            {
                "head": "grokbot/tracker-safe-job",
                "state": "MERGED",
                "url": "https://github.com/example/brigade/pull/123",
                "isDraft": True,
                "headRefOid": "b" * 40,
            }
        ],
    }

    status = cloud_tracker.status_payload(tmp_path, now=NOW, provider_tasks={}, github=github)
    row = next(entry for entry in status["entries"] if entry.get("job_id") == completed)
    assert row["classification"] == "landed"
    assert cloud_tracker.health(tmp_path, now=NOW, github=github)["issue_count"] == 0


def test_grokbot_branch_is_orphaned_and_cloud_sweep_keeps_safe_references(tmp_path: Path):
    status = cloud_tracker.status_payload(
        tmp_path,
        now=NOW,
        github={"branches": [{"name": "grokbot/unregistered"}], "prs": []},
    )
    orphan = next(row for row in status["entries"] if row["id"] == "orphan-branch:grokbot/unregistered")
    assert orphan["provider"] == "grokbot-cloud"
    assert orphan["classification"] == "orphaned"

    _completed_grokbot_job(tmp_path, now=NOW - timedelta(hours=30))
    report = cloud_tracker.sweep(
        tmp_path,
        now=NOW,
        status=cloud_tracker.status_payload(tmp_path, now=NOW, github={"branches": [], "prs": []}),
    )
    assert any(item["id"].startswith("grokbot:") for item in report["recoverable"])
    assert "PRIVATE TRACKER INSTRUCTIONS" not in json.dumps(report)


def test_stale_ready_threshold_configurable(tmp_path: Path):
    cloud_tracker.save_registry(
        tmp_path,
        {
            "schema": cloud_tracker.REGISTRY_SCHEMA,
            "version": 1,
            "stale_ready_hours": 48,
            "entries": [
                {
                    "id": "e-still-ready",
                    "provider": "codex-cloud",
                    "task_id": "t7",
                    "label": "not-stale-yet",
                    "prompt_hash": "sha256:11",
                    "dispatched_at": _iso(NOW - timedelta(hours=30)),
                    "source": "dispatch",
                    "expected_artifact": {"kind": "diff"},
                }
            ],
        },
    )
    status = cloud_tracker.status_payload(
        tmp_path,
        now=NOW,
        provider_tasks={"t7": {"state": "ready", "ready_at": _iso(NOW - timedelta(hours=24))}},
        github={"branches": [], "prs": []},
        cursor_wired=False,
    )
    assert status["stale_ready_hours"] == 48
    assert status["entries"][0]["classification"] == "ready-to-land"


def test_sweep_writes_report_receipt_and_does_not_delete(tmp_path: Path, monkeypatch):
    branch_dir_marker = tmp_path / "branches-still-here"
    branch_dir_marker.mkdir()
    cloud_tracker.register(
        tmp_path,
        provider="codex-cloud",
        task_id="t-sweep",
        label="sweep-me",
        prompt_hash=_prompt_hash("x"),
        branch="codex/sweep-candidate",
        dispatched_at=_iso(NOW - timedelta(hours=40)),
    )

    real_status = cloud_tracker.status_payload
    fixed = real_status(
        tmp_path,
        now=NOW,
        provider_tasks={"t-sweep": {"state": "ready", "ready_at": _iso(NOW - timedelta(hours=30))}},
        github={"branches": [{"name": "codex/sweep-candidate"}], "prs": []},
        cursor_wired=False,
    )
    monkeypatch.setattr(cloud_tracker, "status_payload", lambda *a, **k: fixed)
    report = cloud_tracker.sweep(tmp_path, now=NOW)
    assert report["schema"] == cloud_tracker.SWEEP_SCHEMA
    assert report["action"] == "report-only"
    assert "deletable" in report
    assert "recoverable" in report
    assert branch_dir_marker.is_dir()
    receipt = tmp_path / ".brigade" / "cloud" / "sweeps" / report["sweep_id"] / "sweep.json"
    assert receipt.is_file()
    assert json.loads(receipt.read_text())["action"] == "report-only"


def test_stale_entries_surface_in_work_brief(tmp_path: Path, monkeypatch):
    from brigade.work_cmd.session import briefing

    cloud_tracker.save_registry(
        tmp_path,
        {
            "schema": cloud_tracker.REGISTRY_SCHEMA,
            "version": 1,
            "stale_ready_hours": 6,
            "entries": [
                {
                    "id": "e-brief-stale",
                    "provider": "codex-cloud",
                    "task_id": "t-brief",
                    "label": "stale-for-brief",
                    "prompt_hash": "sha256:22",
                    "dispatched_at": _iso(NOW - timedelta(hours=30)),
                    "source": "dispatch",
                    "expected_artifact": {"kind": "diff"},
                }
            ],
        },
    )

    real_status = cloud_tracker.status_payload

    def fake_status(target, **kwargs):
        return real_status(
            target,
            now=NOW,
            provider_tasks={"t-brief": {"state": "ready", "ready_at": _iso(NOW - timedelta(hours=24))}},
            github={"branches": [], "prs": []},
            cursor_wired=False,
        )

    monkeypatch.setattr(
        cloud_tracker, "observe_providers", lambda target, **k: ({}, {"branches": [], "prs": []}, False)
    )
    monkeypatch.setattr(cloud_tracker, "status_payload", fake_status)
    payload = briefing._brief_payload(tmp_path, limit=1)
    cloud = payload.get("cloud_tracker")
    assert isinstance(cloud, dict)
    assert cloud["stale_count"] >= 1
    assert any(row["classification"] == "stale" for row in cloud.get("stale_entries", []))


def test_grokbot_stale_entry_surfaces_in_work_brief_without_task_text(tmp_path: Path, monkeypatch):
    _completed_grokbot_job(tmp_path, now=NOW - timedelta(hours=30))
    monkeypatch.setattr(
        cloud_tracker,
        "observe_providers",
        lambda target, **kwargs: ({}, {"branches": [], "prs": []}, False),
    )
    from brigade.work_cmd.session import briefing

    payload = briefing._brief_payload(tmp_path, limit=1)
    cloud = payload["cloud_tracker"]
    row = next(item for item in cloud["stale_entries"] if item.get("provider") == "grokbot-cloud")
    assert row["job_id"].startswith("grokbot-")
    assert "PRIVATE TRACKER INSTRUCTIONS" not in json.dumps(payload)


def test_cli_run_cloud_status_json_contract(tmp_path: Path, capsys, monkeypatch):
    cloud_tracker.register(
        tmp_path,
        provider="codex-cloud",
        task_id="t-cli",
        label="cli-status",
        prompt_hash=_prompt_hash("x"),
        dispatched_at=_iso(NOW),
    )
    monkeypatch.setattr(
        cloud_tracker,
        "observe_providers",
        lambda target, **kwargs: (
            {"t-cli": {"state": "running"}},
            {"branches": [], "prs": []},
            False,
        ),
    )
    rc = cli.main(["run", "cloud", "status", "--target", str(tmp_path), "--json"])
    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["schema"] == cloud_tracker.STATUS_SCHEMA
    assert payload["entries"][0]["classification"] == "pending"
    assert payload["sources"]["cursor-cloud"]["wired"] is False


def test_cli_cloud_status_includes_grokbot_without_private_envelope(tmp_path: Path, capsys, monkeypatch):
    job_id = _completed_grokbot_job(tmp_path, now=NOW - timedelta(hours=1))
    monkeypatch.setattr(
        cloud_tracker,
        "observe_providers",
        lambda target, **kwargs: ({}, {"branches": [], "prs": []}, False),
    )

    assert cli.main(["run", "cloud", "status", "--target", str(tmp_path), "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    row = next(item for item in payload["entries"] if item.get("job_id") == job_id)
    assert row["provider"] == "grokbot-cloud"
    assert row["artifact_refs"]["pr_url"].endswith("/pull/123")
    assert "PRIVATE TRACKER INSTRUCTIONS" not in json.dumps(payload)


def test_cli_run_cloud_register_and_adopt(tmp_path: Path, capsys):
    rc = cli.main(
        [
            "run",
            "cloud",
            "register",
            "--target",
            str(tmp_path),
            "--provider",
            "codex-cloud",
            "--task-id",
            "task_reg_1",
            "--label",
            "from-cli",
            "--prompt-hash",
            _prompt_hash("y"),
            "--json",
        ]
    )
    assert rc == 0
    assert json.loads(capsys.readouterr().out)["task_id"] == "task_reg_1"
    rc = cli.main(
        [
            "run",
            "cloud",
            "adopt",
            "--target",
            str(tmp_path),
            "--provider",
            "cursor-cloud",
            "--branch",
            "cursor/adopt-me",
            "--label",
            "orphan-adopt",
            "--json",
        ]
    )
    assert rc == 0
    assert json.loads(capsys.readouterr().out)["source"] == "adopt-branch"


def test_register_on_dispatch_from_codex_cloud(tmp_path: Path, monkeypatch):
    from brigade import codex_cloud, proc

    calls = []

    def fake_run(command, timeout, cwd=None, process_registry=None):
        if command[:3] == ["codex", "cloud", "exec"]:
            return proc.Result(code=0, stdout="task_id: task_dispatch_99\n", stderr="")
        if command[:3] == ["codex", "cloud", "status"]:
            return proc.Result(code=0, stdout="Status: completed\n", stderr="")
        if command[:3] == ["codex", "cloud", "diff"]:
            return proc.Result(code=0, stdout="diff --git a/x b/x\n", stderr="")
        return proc.Result(code=1, stdout="", stderr="unexpected")

    monkeypatch.setattr(codex_cloud.proc, "run", fake_run)
    monkeypatch.setattr(
        cloud_tracker,
        "register",
        lambda *a, **k: calls.append(k) or {"id": "registered"},
    )
    result = codex_cloud.run_cloud_task(
        "do the thing",
        env_id="env-1",
        timeout=60,
        cwd=tmp_path,
        label="dispatch-label",
        register_target=tmp_path,
    )
    assert result.ok
    assert calls
    assert calls[0]["task_id"] == "task_dispatch_99"
    assert calls[0]["provider"] == "codex-cloud"
    assert "prompt" not in calls[0]


def test_codex_observer_uses_bounded_structured_inventory(tmp_path: Path, monkeypatch):
    from brigade import codex_cloud

    monkeypatch.setattr(
        codex_cloud,
        "list_tasks",
        lambda **kwargs: codex_cloud.ListTasksResult(
            [
                {"id": "task_e_structured01", "state": "running", "environment_id": "env-private"},
                {"id": "task_e_structured02", "state": "completed"},
            ]
        ),
    )
    monkeypatch.setattr(
        cloud_tracker,
        "_run_text",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("legacy parser should not run")),
    )

    assert cloud_tracker.observe_codex_cloud_tasks(tmp_path) == {
        "task_e_structured01": {"state": "running", "ready_at": None},
        "task_e_structured02": {"state": "completed", "ready_at": None},
    }


def test_center_cloud_card_uses_registry_rows(tmp_path: Path):
    from brigade.center_cmd.dashboard.views import agent_activity as view

    cloud_tracker.register(
        tmp_path,
        provider="codex-cloud",
        task_id="t-center",
        label="center-row",
        prompt_hash=_prompt_hash("z"),
        dispatched_at=_iso(NOW),
    )
    records = cloud_tracker.center_activity_records(
        tmp_path,
        now=NOW,
        provider_tasks={"t-center": {"state": "ready", "ready_at": _iso(NOW - timedelta(hours=1))}},
        github={"branches": [], "prs": []},
    )
    assert records
    assert all(r["host"] == "cloud" for r in records)
    fragment = view.render(
        {
            "agent_activity_summary": [],
            "agent_activity": records,
            "completed_window_seconds": 3600,
        },
        "nonce",
    )
    assert "cloud tracking not wired" not in fragment.lower()
    assert "center-row" in fragment
    assert 'data-host="cloud"' in fragment


def test_classic_brigade_run_task_still_parses(tmp_path: Path, monkeypatch, capsys):
    # Ensure argv rewrite does not steal normal run tasks that happen to start with words.
    # We only check argparse routing: unknown roster should fail before dispatch, not as cloud.
    rc = cli.main(["run", "not-a-cloud-subcommand", "--cwd", str(tmp_path), "--dry-run"])
    assert rc == 2
    err = capsys.readouterr().err
    assert "cloud" not in err.lower() or "roster" in err.lower() or "error" in err.lower()


def test_provider_normalization_contract_accepts_all_cloud_providers(tmp_path: Path):
    for provider in ("codex-cloud", "cursor-cloud", "grokbot-cloud", "claude-cloud", "jules"):
        entry = cloud_tracker.register(
            tmp_path,
            provider=provider,
            task_id=f"task-{provider}",
            label=f"{provider}-task",
            prompt_hash="sha256:aa",
        )
        assert entry["provider"] == provider


def test_provider_state_normalization_and_active_classification():
    assert cloud_tracker.normalize_provider_state("READY") == "ready"
    assert cloud_tracker.normalize_provider_state("completed") == "completed"
    assert cloud_tracker.normalize_provider_state("FAILED") == "failed"
    assert cloud_tracker.normalize_provider_state("canceled") == "canceled"
    assert cloud_tracker.normalize_provider_state("timed_out") == "timed_out"
    assert cloud_tracker.normalize_provider_state("  ") is None
    assert cloud_tracker.is_active_state("running") is True
    assert cloud_tracker.is_active_state("pending") is True
    assert cloud_tracker.is_active_state("completed") is False
    assert cloud_tracker.is_active_state("failed") is False


def test_github_only_branch_is_orphaned_recovery_never_active_capacity(tmp_path: Path):
    cloud_tracker.save_registry(
        tmp_path,
        {
            "schema": cloud_tracker.REGISTRY_SCHEMA,
            "version": 1,
            "stale_ready_hours": 6,
            "entries": [],
        },
    )
    status = cloud_tracker.status_payload(
        tmp_path,
        now=NOW,
        provider_tasks={},
        github={
            "branches": [
                {"name": "codex/orphan"},
                {"name": "cursor/orphan"},
                {"name": "claude/orphan"},
                {"name": "jules/orphan"},
                {"name": "grokbot/orphan"},
            ],
            "prs": [],
        },
        cursor_wired=False,
    )
    orphans = [e for e in status["entries"] if e["classification"] == "orphaned"]
    assert {o["branch"] for o in orphans} == {
        "codex/orphan",
        "cursor/orphan",
        "claude/orphan",
        "jules/orphan",
        "grokbot/orphan",
    }
    assert all(o["source"] == "github-branch" for o in orphans)


def test_sync_command_exists_and_is_non_mutating(tmp_path: Path, capsys, monkeypatch):
    cloud_tracker.register(
        tmp_path,
        provider="codex-cloud",
        task_id="task-sync",
        label="sync-task",
        prompt_hash="sha256:bb",
        dispatched_at=_iso(NOW),
    )
    observations = {
        provider: cloud_tracker.ProviderObservation(False, False, "unconfigured", {})
        for provider in cloud_tracker.TRACKER_PROVIDERS
    }
    observations["codex-cloud"] = cloud_tracker.ProviderObservation(
        True,
        True,
        None,
        {"task-sync": {"state": "running"}},
    )
    monkeypatch.setattr(
        cloud_tracker,
        "observe_provider_details",
        lambda target, **k: (observations, {"branches": [], "prs": []}),
    )
    rc = cli.main(["run", "cloud", "sync", "--target", str(tmp_path), "--json"])
    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["schema"] == cloud_tracker.SYNC_SCHEMA
    assert payload["action"] == "reconcile"
    assert payload["counts"]["needs_you"] == 0
    assert payload["counts"]["active"] == 1


def test_sync_payload_releases_terminal_row_when_hub_lease_matches(tmp_path: Path, monkeypatch):
    from brigade import fleet_client

    cloud_tracker.register(
        tmp_path,
        provider="codex-cloud",
        task_id="task-sync",
        label="sync-task",
        prompt_hash="sha256:bb",
        dispatched_at=_iso(NOW),
    )
    release_calls: list[dict[str, Any]] = []

    def capture_release(lease_id: str, *, state: str | None = None, holder: str | None = None):
        release_calls.append({"lease_id": lease_id, "state": state, "holder": holder})
        return fleet_client.CloudDecision(True, "ok")

    monkeypatch.setattr(fleet_client, "release_cloud", capture_release)
    payload = cloud_tracker.sync_payload(
        tmp_path,
        now=NOW,
        provider_tasks={"task-sync": {"state": "ready", "ready_at": _iso(NOW)}},
        github={"branches": [], "prs": []},
        cursor_wired=False,
        hub_leases=[{"lease_id": "lease-1", "provider_task_id": "task-sync"}],
    )
    assert payload["schema"] == cloud_tracker.SYNC_SCHEMA
    assert payload["counts"]["released"] == 1
    assert payload["counts"]["needs_you"] == 0
    assert payload["counts"]["active"] == 0
    assert release_calls
    assert release_calls[0]["lease_id"] == "lease-1"


def test_sync_payload_uses_private_holder_and_surfaces_release_refusal(tmp_path: Path, monkeypatch):
    cloud_tracker.register(
        tmp_path,
        provider="codex-cloud",
        task_id="task-fenced",
        label="fenced-task",
        prompt_hash="sha256:bb",
        dispatched_at=_iso(NOW),
        lease_holder="private-fence",
    )
    calls: list[dict[str, Any]] = []

    def refuse_release(lease_id: str, *, state: str | None = None, holder: str | None = None):
        calls.append({"lease_id": lease_id, "state": state, "holder": holder})
        return fleet_client.CloudDecision(False, "refused")

    monkeypatch.setattr(fleet_client, "release_cloud", refuse_release)
    payload = cloud_tracker.sync_payload(
        tmp_path,
        now=NOW,
        provider_tasks={"task-fenced": {"state": "completed"}},
        github={"branches": [], "prs": []},
        cursor_wired=False,
        hub_leases=[{"lease_id": "lease-fenced", "provider_task_id": "task-fenced"}],
    )

    assert calls == [{"lease_id": "lease-fenced", "state": "ready-to-land", "holder": "private-fence"}]
    assert payload["counts"]["released"] == 0
    assert payload["counts"]["needs_you"] == 1
    assert "lease_holder" not in payload["active"]


def test_sync_payload_renews_active_fenced_lease(tmp_path: Path, monkeypatch):
    cloud_tracker.register(
        tmp_path,
        provider="codex-cloud",
        task_id="task-active",
        label="active-task",
        prompt_hash="sha256:bb",
        dispatched_at=_iso(NOW),
        lease_holder="private-fence",
    )
    calls: list[dict[str, Any]] = []

    def renew(lease_id: str, *, ttl_seconds: int = 900, holder: str | None = None):
        calls.append({"lease_id": lease_id, "holder": holder})
        return fleet_client.CloudDecision(True, "ok")

    monkeypatch.setattr(fleet_client, "renew_cloud", renew)
    payload = cloud_tracker.sync_payload(
        tmp_path,
        now=NOW,
        provider_tasks={"task-active": {"state": "running"}},
        github={"branches": [], "prs": []},
        cursor_wired=False,
        hub_leases=[{"lease_id": "lease-active", "provider_task_id": "task-active"}],
    )

    assert calls == [{"lease_id": "lease-active", "holder": "private-fence"}]
    assert payload["counts"]["renewed"] == 1


def test_sync_payload_does_not_invent_needs_you_when_no_hub_lease_matches(tmp_path: Path):
    cloud_tracker.register(
        tmp_path,
        provider="codex-cloud",
        task_id="task-sync",
        label="sync-task",
        prompt_hash="sha256:bb",
        dispatched_at=_iso(NOW),
    )
    payload = cloud_tracker.sync_payload(
        tmp_path,
        now=NOW,
        provider_tasks={"task-sync": {"state": "ready", "ready_at": _iso(NOW)}},
        github={"branches": [], "prs": []},
        cursor_wired=False,
        hub_leases=[],
    )
    assert payload["counts"]["released"] == 0
    assert payload["counts"]["needs_you"] == 0
    assert payload["counts"]["active"] == 0


def test_active_states_include_cursor_creating_and_claude_active():
    assert cloud_tracker.is_active_state("creating") is True
    assert cloud_tracker.is_active_state("active") is True


def test_observe_cursor_cloud_tasks_maps_sanitized_inventory(monkeypatch, tmp_path: Path):
    monkeypatch.setenv("CURSOR_API_KEY", "fake-cursor-key")
    monkeypatch.setattr(
        cursor_cloud,
        "list_agents",
        lambda *a, **k: [
            {"id": "agent-c1", "name": "worker", "latestRunId": "r1", "latestRunState": "RUNNING"},
            {"id": "agent-c2", "name": "reviewer", "latestRunId": "r2", "latestRunState": "FINISHED"},
        ],
    )
    tasks = cloud_tracker.observe_cursor_cloud_tasks(tmp_path)
    assert tasks == {
        "agent-c1": {"state": "running", "agent_id": "agent-c1", "run_id": "r1"},
        "agent-c2": {"state": "finished", "agent_id": "agent-c2", "run_id": "r2"},
    }


def test_observe_cursor_cloud_tasks_stays_bounded_on_api_failure(monkeypatch, tmp_path: Path):
    monkeypatch.setenv("CURSOR_API_KEY", "fake-cursor-key")
    monkeypatch.setattr(
        cursor_cloud, "list_agents", lambda *a, **k: (_ for _ in ()).throw(cursor_cloud.CursorCloudError("boom"))
    )
    cloud_tracker.register(
        tmp_path,
        provider="cursor-cloud",
        task_id="agent-existing",
        label="existing",
        prompt_hash=_prompt_hash("x"),
        dispatched_at=_iso(NOW),
    )
    tasks = cloud_tracker.observe_cursor_cloud_tasks(tmp_path)
    assert tasks == {}
    payload = cloud_tracker.status_payload(
        tmp_path,
        now=NOW,
        provider_tasks=tasks,
        github={"branches": [], "prs": []},
        cursor_wired=True,
    )
    row = next(r for r in payload["entries"] if r["task_id"] == "agent-existing")
    assert row["classification"] == "pending"


def test_status_payload_uses_cursor_inventory_for_registered_task(monkeypatch, tmp_path: Path):
    cloud_tracker.register(
        tmp_path,
        provider="cursor-cloud",
        task_id="agent-c1",
        label="cursor-task",
        prompt_hash=_prompt_hash("x"),
        dispatched_at=_iso(NOW),
    )
    payload = cloud_tracker.status_payload(
        tmp_path,
        now=NOW,
        provider_tasks={"agent-c1": {"state": "creating"}},
        github={"branches": [], "prs": []},
        cursor_wired=True,
    )
    row = next(r for r in payload["entries"] if r["task_id"] == "agent-c1")
    assert row["provider_state"] == "creating"
    assert row["classification"] == "pending"


def test_observe_providers_does_not_call_claude_local_inventory(monkeypatch, tmp_path: Path):
    called = []

    def fail_if_called(*a, **k):
        called.append(True)
        raise RuntimeError("claude local inventory must not be observed as cloud authority")

    _isolate_hosted_providers(monkeypatch)
    monkeypatch.setattr(claude_cloud, "list_agents", fail_if_called)
    tasks, _github, _cursor_wired = cloud_tracker.observe_providers(tmp_path)
    assert tasks == {}
    assert called == []


def test_observe_provider_details_keeps_unconfigured_and_failed_inventory_distinct(monkeypatch, tmp_path: Path):
    """An empty inventory is healthy only when the provider answered it."""
    from brigade import fleet_client_grokbot

    _isolate_hosted_providers(monkeypatch)
    monkeypatch.setenv("CURSOR_API_KEY", "fake-cursor-key")
    monkeypatch.setenv("JULES_API_KEY", "fake-jules-key")
    monkeypatch.setattr(
        cursor_cloud,
        "list_agents",
        lambda *a, **k: (_ for _ in ()).throw(OSError("network unavailable")),
    )
    monkeypatch.setattr(jules_cloud, "list_sessions", lambda *a, **k: [])
    monkeypatch.setattr(
        fleet_client_grokbot,
        "whoami",
        lambda **k: fleet_client_grokbot.GrokbotHubDecision(False, "no-identity"),
    )

    observations, _github = cloud_tracker.observe_provider_details(tmp_path)

    assert observations["cursor-cloud"].configured is True
    assert observations["cursor-cloud"].reachable is False
    assert observations["cursor-cloud"].reason == "transport-failure"
    assert observations["cursor-cloud"].tasks == {}
    assert observations["jules"].configured is True
    assert observations["jules"].reachable is True
    assert observations["jules"].reason is None
    assert observations["jules"].tasks == {}
    assert observations["grokbot-cloud"].configured is True
    assert observations["grokbot-cloud"].reachable is False
    assert observations["grokbot-cloud"].reason == "actor-not-enrolled"
    assert observations["claude-cloud"].reason == "disabled-by-policy"

    payload = cloud_tracker.status_payload(
        tmp_path,
        provider_observations=observations,
        github={"branches": [], "prs": []},
    )
    assert payload["sources"]["cursor-cloud"]["reachable"] is False
    assert payload["sources"]["jules"]["reachable"] is True
    assert payload["sources"]["claude-cloud"]["reason"] == "disabled-by-policy"


def test_provider_auth_failures_are_configured_but_not_reachable(monkeypatch, tmp_path: Path):
    from brigade import fleet_client_grokbot

    _isolate_hosted_providers(monkeypatch)
    monkeypatch.setenv("CURSOR_API_KEY", "fake-cursor-key")
    monkeypatch.setattr(
        cursor_cloud,
        "list_agents",
        lambda *a, **k: (_ for _ in ()).throw(cursor_cloud.CursorCloudError("sanitized", reason="auth-failure")),
    )
    monkeypatch.setattr(
        fleet_client_grokbot,
        "whoami",
        lambda **k: fleet_client_grokbot.GrokbotHubDecision(False, "auth-failed"),
    )

    cursor = cloud_tracker.observe_provider("cursor-cloud", tmp_path)
    grokbot = cloud_tracker.observe_provider("grokbot-cloud", tmp_path)

    assert (cursor.configured, cursor.reachable, cursor.reason) == (
        True,
        False,
        "auth-failure",
    )
    assert (grokbot.configured, grokbot.reachable, grokbot.reason) == (
        True,
        False,
        "auth-failure",
    )


def test_codex_failed_inventory_keeps_health_and_bounded_status_evidence(monkeypatch, tmp_path: Path):
    from brigade import codex_cloud

    cloud_tracker.register(
        tmp_path,
        provider="codex-cloud",
        task_id="task_registered01",
        label="registered",
        prompt_hash=_prompt_hash("x"),
        dispatched_at=_iso(NOW),
    )
    monkeypatch.setattr(
        codex_cloud,
        "list_tasks",
        lambda **kwargs: codex_cloud.ListTasksResult([], ok=False, reason="auth-failure"),
    )
    monkeypatch.setattr(
        cloud_tracker,
        "_run_text",
        lambda *args, **kwargs: (0, "[COMPLETED] registered", ""),
    )

    observation = cloud_tracker.observe_provider("codex-cloud", tmp_path)

    assert (observation.configured, observation.reachable, observation.reason) == (
        True,
        False,
        "auth-failure",
    )
    assert observation.tasks == {"task_registered01": {"state": "completed", "ready_at": None}}


def test_local_claude_session_cannot_make_claude_cloud_registry_row_active(monkeypatch, tmp_path: Path):
    cloud_tracker.register(
        tmp_path,
        provider="claude-cloud",
        task_id="sess-claude-1",
        label="claude-task",
        prompt_hash=_prompt_hash("x"),
        dispatched_at=_iso(NOW),
    )
    _isolate_hosted_providers(monkeypatch)
    monkeypatch.setattr(
        claude_cloud,
        "list_agents",
        lambda *a, **k: [
            {"id": "sess-claude-1", "name": "worker", "state": "active", "workspace": "/home/user/secret"},
        ],
    )
    provider_tasks, _github, _cursor_wired = cloud_tracker.observe_providers(tmp_path)
    assert provider_tasks == {}
    payload = cloud_tracker.status_payload(
        tmp_path,
        now=NOW,
        provider_tasks=provider_tasks,
        github={"branches": [], "prs": []},
        cursor_wired=False,
    )
    row = next(r for r in payload["entries"] if r["task_id"] == "sess-claude-1")
    assert row["provider_state"] is None
    assert row["classification"] == "pending"


def test_status_payload_does_not_leak_claude_cwd_or_prompt_text(monkeypatch, tmp_path: Path, capsys):
    cloud_tracker.register(
        tmp_path,
        provider="claude-cloud",
        task_id="sess-claude-1",
        label="claude-secret-task",
        prompt_hash=_prompt_hash("secret prompt text"),
        dispatched_at=_iso(NOW),
    )
    assert cli.main(["run", "cloud", "status", "--target", str(tmp_path), "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    text = json.dumps(payload)
    assert "/home/user/secret" not in text
    assert "secret prompt text" not in text
    assert "sess-claude-1" in text


@pytest.mark.parametrize(
    ("state", "expected_active", "expected_terminal"),
    [
        ("STATE_UNSPECIFIED", True, False),
        ("QUEUED", True, False),
        ("PLANNING", True, False),
        ("AWAITING_PLAN_APPROVAL", True, False),
        ("AWAITING_USER_FEEDBACK", True, False),
        ("IN_PROGRESS", True, False),
        ("PAUSED", True, False),
        ("FAILED", False, True),
        ("COMPLETED", False, True),
    ],
)
def test_jules_states_hold_capacity_until_terminal(state, expected_active, expected_terminal):
    normalized = cloud_tracker.normalize_provider_state(state)
    assert cloud_tracker.is_active_state(normalized) is expected_active
    assert cloud_tracker.is_terminal_state(normalized) is expected_terminal


def test_jules_cloud_registry_entry_classifies_terminal_via_provider_tasks(tmp_path: Path):
    cloud_tracker.register(
        tmp_path,
        provider="jules",
        task_id="sess-jules-1",
        label="jules:owner/repo@0011223344ff",
        prompt_hash=_prompt_hash("x"),
        dispatched_at=_iso(NOW),
    )
    payload = cloud_tracker.status_payload(
        tmp_path,
        now=NOW,
        provider_tasks={"sess-jules-1": {"state": "completed"}},
        github={"branches": [], "prs": []},
        cursor_wired=False,
    )
    row = next(r for r in payload["entries"] if r["task_id"] == "sess-jules-1")
    assert row["provider_state"] == "completed"
    # `completed` is a READY state. With no expected artifact to miss and no
    # staleness yet, a fresh terminal Jules session is landable, not pending.
    assert row["classification"] == "ready-to-land"


def test_jules_terminal_entry_expecting_a_missing_branch_needs_investigation(tmp_path: Path):
    cloud_tracker.register(
        tmp_path,
        provider="jules",
        task_id="sess-jules-1b",
        label="jules:owner/repo@0011223344ff",
        prompt_hash=_prompt_hash("x"),
        expected_artifact={"kind": "branch", "pattern": "jules/*"},
        dispatched_at=_iso(NOW),
    )
    payload = cloud_tracker.status_payload(
        tmp_path,
        now=NOW,
        provider_tasks={"sess-jules-1b": {"state": "completed"}},
        github={"branches": [], "prs": []},
        cursor_wired=False,
    )
    row = next(r for r in payload["entries"] if r["task_id"] == "sess-jules-1b")
    assert row["classification"] == "needs-investigation"


def test_jules_cloud_registry_entry_with_branch_classifies_ready(tmp_path: Path):
    cloud_tracker.register(
        tmp_path,
        provider="jules",
        task_id="sess-jules-2",
        label="jules:owner/repo@aabbccdd0011",
        prompt_hash=_prompt_hash("x"),
        branch="jules/ready-fix",
        expected_artifact={"kind": "branch", "pattern": "jules/*"},
        dispatched_at=_iso(NOW),
    )
    payload = cloud_tracker.status_payload(
        tmp_path,
        now=NOW,
        provider_tasks={"sess-jules-2": {"state": "completed", "ready_at": _iso(NOW)}},
        github={"branches": [{"name": "jules/ready-fix"}], "prs": []},
        cursor_wired=False,
    )
    row = next(r for r in payload["entries"] if r["task_id"] == "sess-jules-2")
    assert row["provider_state"] == "completed"
    assert row["classification"] == "ready-to-land"


def test_observe_providers_includes_jules_inventory_when_key_present(monkeypatch, tmp_path: Path):
    _isolate_hosted_providers(monkeypatch)
    monkeypatch.setenv("JULES_API_KEY", "fake-jules-key")
    monkeypatch.setattr(
        jules_cloud,
        "list_sessions",
        lambda *a, **k: [
            {"id": "sess-jules-3", "state": "IN_PROGRESS"},
            {"id": "sess-jules-4", "state": "COMPLETED"},
        ],
    )
    provider_tasks, _github, _cursor_wired = cloud_tracker.observe_providers(tmp_path)
    assert provider_tasks == {
        "sess-jules-3": {"state": "in_progress"},
        "sess-jules-4": {"state": "completed"},
    }


def test_observe_providers_skips_jules_inventory_when_key_absent(monkeypatch, tmp_path: Path):
    _isolate_hosted_providers(monkeypatch)
    monkeypatch.delenv("JULES_API_KEY", raising=False)
    monkeypatch.setattr(
        jules_cloud,
        "list_sessions",
        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("must not be called")),
    )
    provider_tasks, _github, _cursor_wired = cloud_tracker.observe_providers(tmp_path)
    assert provider_tasks == {}


def test_sync_payload_releases_terminal_jules_session(monkeypatch, tmp_path: Path):
    cloud_tracker.register(
        tmp_path,
        provider="jules",
        task_id="sess-jules-5",
        label="jules:owner/repo@ffeeddcc9988",
        prompt_hash=_prompt_hash("x"),
        dispatched_at=_iso(NOW),
    )
    release_calls: list[dict[str, Any]] = []

    def capture_release(lease_id: str, *, state: str | None = None, holder: str | None = None):
        release_calls.append({"lease_id": lease_id, "state": state, "holder": holder})
        return fleet_client.CloudDecision(True, "ok")

    monkeypatch.setattr(fleet_client, "release_cloud", capture_release)
    payload = cloud_tracker.sync_payload(
        tmp_path,
        now=NOW,
        provider_tasks={"sess-jules-5": {"state": "completed"}},
        github={"branches": [], "prs": []},
        cursor_wired=False,
        hub_leases=[{"lease_id": "lease-jules-1", "provider_task_id": "sess-jules-5"}],
    )
    assert payload["schema"] == cloud_tracker.SYNC_SCHEMA
    assert payload["counts"]["released"] == 1
    assert payload["counts"]["needs_you"] == 0
    assert payload["counts"]["active"] == 0
    assert release_calls
    assert release_calls[0]["lease_id"] == "lease-jules-1"


def test_status_payload_jules_authority_reflects_key_presence(monkeypatch, tmp_path: Path):
    monkeypatch.setenv("JULES_API_KEY", "fake-jules-key")
    monkeypatch.setattr(cloud_tracker, "observe_jules_cloud_tasks", lambda *a, **k: {})
    payload = cloud_tracker.status_payload(tmp_path)
    assert payload["sources"]["jules"]["wired"] is True
    assert payload["sources"]["jules"]["authority"] == "alpha REST"
    assert payload["sources"]["jules"].get("detail") is None

    monkeypatch.delenv("JULES_API_KEY", raising=False)
    payload = cloud_tracker.status_payload(tmp_path)
    assert payload["sources"]["jules"]["wired"] is False
    assert payload["sources"]["jules"]["authority"] == "unwired"
    assert payload["sources"]["jules"]["detail"] == "JULES_API_KEY not set"


def test_observe_jules_inventory_normalizes_provider_state(monkeypatch, tmp_path: Path):
    monkeypatch.setenv("JULES_API_KEY", "fake-jules-key")
    monkeypatch.setattr(
        jules_cloud,
        "list_sessions",
        lambda *a, **k: [
            {"id": "sess-jules-norm", "state": "AWAITING_PLAN_APPROVAL"},
            {"id": "sess-jules-none", "state": None},
            "not-a-dict",
            {"state": "QUEUED"},
        ],
    )
    tasks = cloud_tracker.observe_jules_cloud_tasks(tmp_path)
    assert tasks == {
        "sess-jules-norm": {"state": "awaiting_plan_approval"},
        "sess-jules-none": {"state": None},
    }


def _register_jules_pair(tmp_path: Path) -> None:
    for task_id in ("sess-jules-seen", "sess-jules-unseen"):
        cloud_tracker.register(
            tmp_path,
            provider="jules",
            task_id=task_id,
            label=f"jules:owner/repo@{'0' * 12}",
            prompt_hash=_prompt_hash("x"),
            dispatched_at=_iso(NOW),
        )


def _jules_hub_leases() -> list[dict[str, Any]]:
    return [
        {"lease_id": "lease-seen", "provider_task_id": "sess-jules-seen"},
        {"lease_id": "lease-unseen", "provider_task_id": "sess-jules-unseen"},
    ]


def test_truncated_jules_inventory_never_releases_capacity_by_absence(monkeypatch, tmp_path: Path):
    """A page walk that stops early omits ids; absence must not read as terminal."""
    _register_jules_pair(tmp_path)
    monkeypatch.setenv("JULES_API_KEY", "fake-jules-key")
    # max_items truncation drops sess-jules-unseen from the observed inventory.
    monkeypatch.setattr(
        jules_cloud, "list_sessions", lambda *a, **k: [{"id": "sess-jules-seen", "state": "in_progress"}]
    )
    release_calls: list[dict[str, Any]] = []

    def capture_release(lease_id: str, *, state: str | None = None, holder: str | None = None):
        release_calls.append({"lease_id": lease_id, "state": state})
        return fleet_client.CloudDecision(True, "ok")

    monkeypatch.setattr(fleet_client, "release_cloud", capture_release)
    payload = cloud_tracker.sync_payload(
        tmp_path,
        now=NOW,
        provider_tasks=cloud_tracker.observe_jules_cloud_tasks(tmp_path),
        github={"branches": [], "prs": []},
        cursor_wired=False,
        hub_leases=_jules_hub_leases(),
    )
    assert release_calls == []
    assert payload["counts"]["released"] == 0
    assert payload["counts"]["active"] == 1


def test_unknown_jules_inventory_never_releases_capacity_by_absence(monkeypatch, tmp_path: Path):
    """A failed provider fetch yields no inventory, which must release nothing."""
    _register_jules_pair(tmp_path)
    monkeypatch.setenv("JULES_API_KEY", "fake-jules-key")
    monkeypatch.setattr(
        jules_cloud,
        "list_sessions",
        lambda *a, **k: (_ for _ in ()).throw(jules_cloud.JulesCloudError("boom")),
    )
    release_calls: list[str] = []

    def capture_release(lease_id: str, *, state: str | None = None, holder: str | None = None):
        release_calls.append(lease_id)
        return fleet_client.CloudDecision(True, "ok")

    monkeypatch.setattr(fleet_client, "release_cloud", capture_release)
    payload = cloud_tracker.sync_payload(
        tmp_path,
        now=NOW,
        provider_tasks=cloud_tracker.observe_jules_cloud_tasks(tmp_path),
        github={"branches": [], "prs": []},
        cursor_wired=False,
        hub_leases=_jules_hub_leases(),
    )
    assert release_calls == []
    assert payload["counts"]["released"] == 0


def test_lease_label_is_bounded_and_prompt_free():
    prompt = "rotate the production credentials for acme corp"
    digest = cloud_tracker.prompt_hash(prompt)
    label = cloud_tracker.lease_label("jules", "https://github.com/owner/repo", digest)
    assert label == f"jules:owner/repo@{digest.removeprefix('sha256:')[:12]}"
    assert len(label) <= cloud_tracker.LEASE_LABEL_MAX
    for word in prompt.split():
        assert word not in label


@pytest.mark.parametrize(
    ("provider", "repo", "digest", "expected"),
    [
        ("cursor-cloud", "owner/repo", "sha256:abcdef0123456789", "cursor-cloud:owner/repo@abcdef012345"),
        ("jules", None, None, "jules:unknown-repo@nohash"),
        ("jules", "https://evil.example.com/a/b", "sha256:aa", "jules:unknown-repo@aa"),
        ("", "owner/repo", "not-a-hash", "unknown:owner/repo@nohash"),
        ("jules", "owner/repo/extra", "sha256:bb", "jules:unknown-repo@bb"),
    ],
)
def test_lease_label_rejects_unsafe_inputs(provider, repo, digest, expected):
    assert cloud_tracker.lease_label(provider, repo, digest) == expected


def test_lease_label_truncates_pathological_input():
    label = cloud_tracker.lease_label("x" * 400, "owner/repo", "sha256:aa")
    assert len(label) == cloud_tracker.LEASE_LABEL_MAX


def _seed_mixed_registry(tmp_path: Path) -> dict[str, str]:
    """Seed one row per classification plus an ambiguous active row."""
    ids = {
        "pending": "e-pending-keep",
        "ready": "e-ready-keep",
        "stale": "e-stale-keep",
        "orphaned": "e-orphan-keep",
        "needs": "e-needs-keep",
        "ambiguous": "e-ambiguous-keep",
        "landed-new": "e-landed-new",
        "landed-mid": "e-landed-mid",
        "landed-old": "e-landed-old",
        "landed-oldest": "e-landed-oldest",
    }
    cloud_tracker.save_registry(
        tmp_path,
        {
            "schema": cloud_tracker.REGISTRY_SCHEMA,
            "version": 1,
            "stale_ready_hours": 6,
            "entries": [
                {
                    "id": ids["pending"],
                    "provider": "codex-cloud",
                    "task_id": "t-pending",
                    "label": "pending-row",
                    "prompt_hash": "sha256:aa",
                    "dispatched_at": _iso(NOW - timedelta(hours=1)),
                    "source": "dispatch",
                    "expected_artifact": {"kind": "diff"},
                },
                {
                    "id": ids["ready"],
                    "provider": "codex-cloud",
                    "task_id": "t-ready",
                    "label": "ready-row",
                    "prompt_hash": "sha256:bb",
                    "dispatched_at": _iso(NOW - timedelta(hours=2)),
                    "source": "dispatch",
                    "expected_artifact": {"kind": "branch", "pattern": "codex/*"},
                    "branch": "codex/ready-keep",
                },
                {
                    "id": ids["stale"],
                    "provider": "codex-cloud",
                    "task_id": "t-stale",
                    "label": "stale-row",
                    "prompt_hash": "sha256:cc",
                    "dispatched_at": _iso(NOW - timedelta(hours=30)),
                    "source": "dispatch",
                    "expected_artifact": {"kind": "diff"},
                },
                {
                    "id": ids["orphaned"],
                    "provider": "codex-cloud",
                    "task_id": "t-orphan",
                    "label": "orphan-row",
                    "prompt_hash": "sha256:dd",
                    "dispatched_at": _iso(NOW - timedelta(hours=40)),
                    "source": "dispatch",
                    "expected_artifact": {"kind": "branch", "pattern": "codex/*"},
                    "branch": "codex/orphan-keep",
                },
                {
                    "id": ids["needs"],
                    "provider": "codex-cloud",
                    "task_id": "t-needs",
                    "label": "needs-row",
                    "prompt_hash": "sha256:ee",
                    "dispatched_at": _iso(NOW - timedelta(hours=50)),
                    "source": "dispatch",
                    "expected_artifact": {"kind": "branch", "pattern": "codex/*"},
                    "branch": "codex/missing-keep",
                },
                {
                    "id": ids["ambiguous"],
                    "provider": "cursor-cloud",
                    "task_id": "t-ambiguous",
                    "label": "ambiguous-row",
                    "prompt_hash": "sha256:ff",
                    "dispatched_at": _iso(NOW - timedelta(hours=80)),
                    "source": "dispatch",
                    "expected_artifact": {"kind": "diff"},
                },
                {
                    "id": ids["landed-new"],
                    "provider": "codex-cloud",
                    "task_id": "t-landed-new",
                    "label": "landed-new",
                    "prompt_hash": "sha256:11",
                    "dispatched_at": _iso(NOW - timedelta(hours=3)),
                    "source": "dispatch",
                    "expected_artifact": {"kind": "branch", "pattern": "codex/*"},
                    "branch": "codex/landed-new",
                },
                {
                    "id": ids["landed-mid"],
                    "provider": "codex-cloud",
                    "task_id": "t-landed-mid",
                    "label": "landed-mid",
                    "prompt_hash": "sha256:22",
                    "dispatched_at": _iso(NOW - timedelta(hours=10)),
                    "source": "dispatch",
                    "expected_artifact": {"kind": "branch", "pattern": "codex/*"},
                    "branch": "codex/landed-mid",
                },
                {
                    "id": ids["landed-old"],
                    "provider": "codex-cloud",
                    "task_id": "t-landed-old",
                    "label": "landed-old",
                    "prompt_hash": "sha256:33",
                    "dispatched_at": _iso(NOW - timedelta(hours=200)),
                    "source": "dispatch",
                    "expected_artifact": {"kind": "branch", "pattern": "codex/*"},
                    "branch": "codex/landed-old",
                },
                {
                    "id": ids["landed-oldest"],
                    "provider": "codex-cloud",
                    "task_id": "t-landed-oldest",
                    "label": "landed-oldest",
                    "prompt_hash": "sha256:44",
                    "dispatched_at": _iso(NOW - timedelta(hours=400)),
                    "source": "dispatch",
                    "expected_artifact": {"kind": "branch", "pattern": "codex/*"},
                    "branch": "codex/landed-oldest",
                },
            ],
        },
    )
    return ids


def _mixed_provider_and_github() -> tuple[dict[str, Any], dict[str, Any]]:
    provider_tasks = {
        "t-pending": {"state": "running"},
        "t-ready": {"state": "ready", "ready_at": _iso(NOW - timedelta(hours=1))},
        "t-stale": {"state": "ready", "ready_at": _iso(NOW - timedelta(hours=24))},
        "t-orphan": {"state": "failed"},
        "t-needs": {"state": "finished"},
        "t-landed-new": {"state": "completed"},
        "t-landed-mid": {"state": "completed"},
        "t-landed-old": {"state": "completed"},
        "t-landed-oldest": {"state": "completed"},
    }
    github = {
        "branches": [
            {"name": "codex/ready-keep"},
            {"name": "codex/orphan-keep"},
        ],
        "prs": [
            {"head": "codex/ready-keep", "state": "OPEN", "number": 1},
            {"head": "codex/landed-new", "state": "MERGED", "number": 2},
            {"head": "codex/landed-mid", "state": "MERGED", "number": 3},
            {"head": "codex/landed-old", "state": "MERGED", "number": 4},
            {"head": "codex/landed-oldest", "state": "MERGED", "number": 5},
        ],
    }
    return provider_tasks, github


def test_status_never_compacts_or_rewrites_registry(tmp_path: Path):
    ids = _seed_mixed_registry(tmp_path)
    provider_tasks, github = _mixed_provider_and_github()
    path = cloud_tracker.registry_path(tmp_path)
    before = path.read_bytes()
    checksum = hashlib.sha256(before).hexdigest()
    mtime = path.stat().st_mtime_ns

    status = cloud_tracker.status_payload(
        tmp_path,
        now=NOW,
        provider_tasks=provider_tasks,
        github=github,
        cursor_wired=False,
    )

    assert status["schema"] == cloud_tracker.STATUS_SCHEMA
    assert path.read_bytes() == before
    assert hashlib.sha256(path.read_bytes()).hexdigest() == checksum
    assert path.stat().st_mtime_ns == mtime
    stored_ids = {entry["id"] for entry in cloud_tracker.load_registry(tmp_path)["entries"]}
    assert stored_ids == set(ids.values())


def test_compact_preserves_active_ambiguous_orphaned_and_needs_investigation(tmp_path: Path):
    ids = _seed_mixed_registry(tmp_path)
    provider_tasks, github = _mixed_provider_and_github()
    report = cloud_tracker.compact_registry(
        tmp_path,
        now=NOW,
        keep_terminal=1,
        max_age_hours=24,
        provider_tasks=provider_tasks,
        github=github,
        cursor_wired=False,
    )
    kept_ids = {entry["id"] for entry in cloud_tracker.load_registry(tmp_path)["entries"]}
    assert ids["pending"] in kept_ids
    assert ids["ready"] in kept_ids
    assert ids["stale"] in kept_ids
    assert ids["orphaned"] in kept_ids
    assert ids["needs"] in kept_ids
    assert ids["ambiguous"] in kept_ids
    assert report["schema"] == cloud_tracker.MAINTENANCE_SCHEMA
    assert report["action"] == "compact"
    assert report["policy"]["preserve"] == [
        "active",
        "ambiguous",
        "orphaned",
        "needs-investigation",
    ]


def test_compact_bounds_terminal_landed_history_deterministically(tmp_path: Path):
    ids = _seed_mixed_registry(tmp_path)
    provider_tasks, github = _mixed_provider_and_github()
    report = cloud_tracker.compact_registry(
        tmp_path,
        now=NOW,
        keep_terminal=1,
        max_age_hours=24,
        provider_tasks=provider_tasks,
        github=github,
        cursor_wired=False,
    )
    kept_ids = {entry["id"] for entry in cloud_tracker.load_registry(tmp_path)["entries"]}
    assert ids["landed-new"] in kept_ids
    assert ids["landed-mid"] not in kept_ids
    assert ids["landed-old"] not in kept_ids
    assert ids["landed-oldest"] not in kept_ids
    assert report["counts"]["dropped"] == 3
    assert report["dropped_ids"] == [ids["landed-mid"], ids["landed-old"], ids["landed-oldest"]]
    assert report["policy"]["keep_terminal"] == 1
    assert report["policy"]["max_age_hours"] == 24
    assert report["policy"]["sort"] == ["dispatched_at_desc", "id_desc"]


def test_compact_writes_auditable_receipt_and_uses_atomic_write(tmp_path: Path, monkeypatch):
    _seed_mixed_registry(tmp_path)
    provider_tasks, github = _mixed_provider_and_github()
    writes: list[Path] = []
    real_write = cloud_tracker.localio.write_json

    def capture_write(path, payload):
        writes.append(Path(path))
        return real_write(path, payload)

    monkeypatch.setattr(cloud_tracker.localio, "write_json", capture_write)
    report = cloud_tracker.compact_registry(
        tmp_path,
        now=NOW,
        keep_terminal=1,
        max_age_hours=24,
        provider_tasks=provider_tasks,
        github=github,
        cursor_wired=False,
    )
    receipt = tmp_path / ".brigade" / "cloud" / "maintenance" / report["maintenance_id"] / "compact.json"
    assert receipt.is_file()
    stored = json.loads(receipt.read_text())
    assert stored["schema"] == cloud_tracker.MAINTENANCE_SCHEMA
    assert stored["policy"] == report["policy"]
    assert stored["atomic"] is True
    assert cloud_tracker.registry_path(tmp_path) in writes
    assert receipt in writes


def test_cloud_dispatch_and_status_leave_checkout_registry_checksum_and_mtime_unchanged(
    tmp_path: Path, capsys, monkeypatch
):
    checkout = Path(__file__).resolve().parents[1]
    checkout_registry = checkout / ".brigade" / "cloud" / "registry.json"
    existed = checkout_registry.is_file()
    checksum = hashlib.sha256(checkout_registry.read_bytes()).hexdigest() if existed else None
    mtime = checkout_registry.stat().st_mtime_ns if existed else None

    monkeypatch.setattr(
        cloud_tracker,
        "observe_providers",
        lambda target, **kwargs: ({"task-isolated": {"state": "running"}}, {"branches": [], "prs": []}, False),
    )
    cloud_tracker.register(
        tmp_path,
        provider="codex-cloud",
        task_id="task-isolated",
        label="isolated-dispatch",
        prompt_hash=_prompt_hash("never write this prompt to checkout"),
        dispatched_at=_iso(NOW),
    )
    assert cli.main(["run", "cloud", "status", "--target", str(tmp_path), "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["entries"][0]["task_id"] == "task-isolated"
    assert cli.main(["run", "cloud", "compact", "--target", str(tmp_path), "--json"]) == 0

    if existed:
        assert hashlib.sha256(checkout_registry.read_bytes()).hexdigest() == checksum
        assert checkout_registry.stat().st_mtime_ns == mtime
        assert "never write this prompt to checkout" not in checkout_registry.read_text()
    else:
        assert not checkout_registry.exists()


def test_observe_cursor_retains_agent_run_identity_and_safe_usage(monkeypatch, tmp_path: Path):
    monkeypatch.setenv("CURSOR_API_KEY", "fake-cursor-key")
    monkeypatch.setattr(
        cursor_cloud,
        "list_agents",
        lambda *a, **k: [
            {
                "id": "agent-c1",
                "name": "secret prompt title",
                "latestRunId": "run-c1",
                "latestRunState": "RUNNING",
                "duration_ms": 12357,
                "updated_at": "2026-08-26T12:00:00Z",
                "usage": {
                    "input_tokens": 10,
                    "output_tokens": 4,
                    "cache_write_tokens": 1,
                    "cache_read_tokens": 2,
                    "total_tokens": 17,
                },
            }
        ],
    )
    tasks = cloud_tracker.observe_cursor_cloud_tasks(tmp_path)
    assert tasks == {
        "agent-c1": {
            "state": "running",
            "agent_id": "agent-c1",
            "run_id": "run-c1",
            "duration_ms": 12357,
            "updated_at": "2026-08-26T12:00:00Z",
            "usage": {
                "input_tokens": 10,
                "output_tokens": 4,
                "cache_write_tokens": 1,
                "cache_read_tokens": 2,
                "total_tokens": 17,
            },
        }
    }
    blob = json.dumps(tasks)
    assert "secret prompt title" not in blob
    assert "fake-cursor-key" not in blob


def test_observe_cursor_drops_prompts_credentials_and_secret_urls(monkeypatch, tmp_path: Path):
    monkeypatch.setenv("CURSOR_API_KEY", "cursor-live-secret")
    monkeypatch.setattr(
        cursor_cloud,
        "list_agents",
        lambda *a, **k: [
            {
                "id": "agent-c9",
                "name": "rotate production credentials",
                "latestRunId": "run-c9",
                "latestRunState": "FINISHED",
                "result": "Added the secret prompt reply",
                "artifact_url": "https://cloud-agent-artifacts.s3.us-east-1.amazonaws.com/x?X-Amz-Signature=deadbeef",
                "usage": {
                    "input_tokens": 3,
                    "output_tokens": 1,
                    "cache_write_tokens": 0,
                    "cache_read_tokens": 0,
                    "total_tokens": 4,
                    "presignedArtifactUrl": "https://secret.example/token",
                },
            }
        ],
    )
    tasks = cloud_tracker.observe_cursor_cloud_tasks(tmp_path)
    blob = json.dumps(tasks)
    assert "rotate production credentials" not in blob
    assert "secret prompt reply" not in blob
    assert "X-Amz-Signature" not in blob
    assert "cursor-live-secret" not in blob
    assert "presignedArtifactUrl" not in blob
    assert tasks["agent-c9"]["agent_id"] == "agent-c9"
    assert tasks["agent-c9"]["run_id"] == "run-c9"
    assert tasks["agent-c9"]["usage"] == {
        "input_tokens": 3,
        "output_tokens": 1,
        "cache_write_tokens": 0,
        "cache_read_tokens": 0,
        "total_tokens": 4,
    }


def test_truncated_cursor_inventory_never_infers_terminal_from_absence(monkeypatch, tmp_path: Path):
    cloud_tracker.register(
        tmp_path,
        provider="cursor-cloud",
        task_id="agent-seen",
        label="cursor:owner/repo@001122334455",
        prompt_hash=_prompt_hash("x"),
        dispatched_at=_iso(NOW),
    )
    cloud_tracker.register(
        tmp_path,
        provider="cursor-cloud",
        task_id="agent-unseen",
        label="cursor:owner/repo@aabbccddeeff",
        prompt_hash=_prompt_hash("x"),
        dispatched_at=_iso(NOW),
    )
    monkeypatch.setenv("CURSOR_API_KEY", "fake-cursor-key")
    monkeypatch.setattr(
        cursor_cloud,
        "list_agents",
        lambda *a, **k: [{"id": "agent-seen", "latestRunId": "run-seen", "latestRunState": "RUNNING"}],
    )
    release_calls: list[str] = []

    def capture_release(lease_id: str, *, state: str | None = None, holder: str | None = None):
        release_calls.append(lease_id)
        return fleet_client.CloudDecision(True, "ok")

    monkeypatch.setattr(fleet_client, "release_cloud", capture_release)
    payload = cloud_tracker.sync_payload(
        tmp_path,
        now=NOW,
        provider_tasks=cloud_tracker.observe_cursor_cloud_tasks(tmp_path),
        github={"branches": [], "prs": []},
        cursor_wired=True,
        hub_leases=[
            {"lease_id": "lease-seen", "provider_task_id": "agent-seen"},
            {"lease_id": "lease-unseen", "provider_task_id": "agent-unseen"},
        ],
    )
    assert release_calls == []
    assert payload["counts"]["released"] == 0
    status = cloud_tracker.status_payload(
        tmp_path,
        now=NOW,
        provider_tasks=cloud_tracker.observe_cursor_cloud_tasks(tmp_path),
        github={"branches": [], "prs": []},
        cursor_wired=True,
    )
    unseen_row = next(row for row in status["entries"] if row["task_id"] == "agent-unseen")
    assert unseen_row["provider_state"] is None
    assert unseen_row["classification"] == "pending"


def test_cli_run_cloud_launch_cursor_json_omits_holder_and_prompt(tmp_path: Path, capsys, monkeypatch):
    prompt = "secret launch prompt text"
    prompt_file = tmp_path / "prompt.txt"
    prompt_file.write_text(prompt, encoding="utf-8")
    prompt_file.chmod(0o600)
    _isolate_hosted_providers(monkeypatch)
    monkeypatch.setenv("CURSOR_API_KEY", "cursor-api-key-deadbeef")
    captured: dict[str, Any] = {}

    def fake_launch(api_key: str, **kwargs: Any):
        captured["api_key"] = api_key
        captured.update(kwargs)
        cloud_tracker.register(
            kwargs["register_target"],
            provider="cursor-cloud",
            task_id="agent-cli",
            label=kwargs["label"],
            prompt_hash=cloud_tracker.prompt_hash(kwargs["prompt"]),
            expected_artifact={"kind": "diff"},
            lease_holder="private-holder",
        )
        return cursor_cloud.LaunchResult(ok=True, agent_id="agent-cli", run_id="run-cli", reason="ok")

    monkeypatch.setattr(cursor_cloud, "launch_agent", fake_launch)
    rc = cli.main(
        [
            "run",
            "cloud",
            "launch",
            "--target",
            str(tmp_path),
            "--provider",
            "cursor-cloud",
            "--repo",
            "owner/repo",
            "--label",
            "cli-launch",
            "--prompt-file",
            str(prompt_file),
            "--json",
        ]
    )
    assert rc == 0
    out = capsys.readouterr()
    payload = json.loads(out.out)
    assert payload["ok"] is True
    assert payload["provider"] == "cursor-cloud"
    assert payload["task_id"] == "agent-cli"
    assert payload["run_id"] == "run-cli"
    assert payload["prompt_hash"] == _prompt_hash(prompt)
    assert payload["label"] == "cli-launch"
    assert "holder" not in payload
    assert "lease_holder" not in payload
    assert prompt not in out.out
    assert prompt not in out.err
    assert captured["api_key"] == "cursor-api-key-deadbeef"
    assert captured["repo"] == "owner/repo"
    assert captured["prompt"] == prompt
    assert captured["auto_create_pr"] is False
    assert captured["register_target"] == tmp_path
    entry = cloud_tracker.load_registry(tmp_path)["entries"][0]
    assert entry["lease_holder"] == "private-holder"
    assert prompt not in json.dumps(entry)


def test_cli_run_cloud_launch_jules_forwards_provider_options(tmp_path: Path, capsys, monkeypatch):
    prompt_file = tmp_path / "prompt.txt"
    prompt_file.write_text("jules prompt", encoding="utf-8")
    prompt_file.chmod(0o600)
    monkeypatch.setenv("JULES_API_KEY", "jules-api-key-deadbeef")
    captured: dict[str, Any] = {}

    def fake_launch(api_key: str, **kwargs: Any):
        captured["api_key"] = api_key
        captured.update(kwargs)
        return jules_cloud.LaunchResult(
            ok=True,
            session_id="sess-cli",
            reason="ok",
            source_name="sources/github-owner-repo",
            starting_branch="dev",
        )

    monkeypatch.setattr(jules_cloud, "launch_agent", fake_launch)
    rc = cli.main(
        [
            "run",
            "cloud",
            "launch",
            "--target",
            str(tmp_path),
            "--provider",
            "jules",
            "--repo",
            "owner/repo",
            "--label",
            "jules-launch",
            "--prompt-file",
            str(prompt_file),
            "--starting-branch",
            "dev",
            "--title",
            "feature work",
            "--auto-create-pr",
            "--json",
        ]
    )
    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["ok"] is True
    assert payload["task_id"] == "sess-cli"
    assert payload["session_id"] == "sess-cli"
    assert payload["starting_branch"] == "dev"
    assert "holder" not in payload
    assert captured["starting_branch"] == "dev"
    assert captured["title"] == "feature work"
    assert captured["auto_create_pr"] is True
    assert captured["register_target"] == tmp_path


def test_cli_run_cloud_launch_missing_key_makes_zero_provider_mutation(tmp_path: Path, capsys, monkeypatch):
    prompt_file = tmp_path / "prompt.txt"
    prompt_file.write_text("unused prompt", encoding="utf-8")
    prompt_file.chmod(0o600)
    _isolate_hosted_providers(monkeypatch)
    calls: list[dict[str, Any]] = []
    monkeypatch.setattr(
        cursor_cloud,
        "launch_agent",
        lambda *a, **k: (
            calls.append({"args": a, "kwargs": k}) or cursor_cloud.LaunchResult(ok=True, agent_id="nope", reason="ok")
        ),
    )
    rc = cli.main(
        [
            "run",
            "cloud",
            "launch",
            "--target",
            str(tmp_path),
            "--provider",
            "cursor-cloud",
            "--repo",
            "owner/repo",
            "--label",
            "missing-key",
            "--prompt-file",
            str(prompt_file),
            "--json",
        ]
    )
    assert rc == 2
    payload = json.loads(capsys.readouterr().out)
    assert payload["ok"] is False
    assert payload["reason"] == "missing-key"
    assert calls == []
    assert cloud_tracker.load_registry(tmp_path)["entries"] == []


def test_cli_run_cloud_launch_bad_prompt_file_makes_zero_provider_mutation(tmp_path: Path, capsys, monkeypatch):
    monkeypatch.setenv("JULES_API_KEY", "jules-api-key-deadbeef")
    calls: list[dict[str, Any]] = []
    monkeypatch.setattr(
        jules_cloud,
        "launch_agent",
        lambda *a, **k: (
            calls.append({"args": a, "kwargs": k}) or jules_cloud.LaunchResult(ok=True, session_id="nope", reason="ok")
        ),
    )
    rc = cli.main(
        [
            "run",
            "cloud",
            "launch",
            "--target",
            str(tmp_path),
            "--provider",
            "jules",
            "--repo",
            "owner/repo",
            "--label",
            "bad-file",
            "--prompt-file",
            str(tmp_path / "missing-prompt.txt"),
            "--json",
        ]
    )
    assert rc == 2
    payload = json.loads(capsys.readouterr().out)
    assert payload["ok"] is False
    assert payload["reason"] == "bad-prompt-file"
    assert calls == []
    assert cloud_tracker.load_registry(tmp_path)["entries"] == []


def test_cli_run_cloud_launch_failure_does_not_register(tmp_path: Path, capsys, monkeypatch):
    prompt_file = tmp_path / "prompt.txt"
    prompt_file.write_text("do not persist this", encoding="utf-8")
    prompt_file.chmod(0o600)
    monkeypatch.setenv("CURSOR_API_KEY", "cursor-api-key-deadbeef")
    monkeypatch.setattr(
        cursor_cloud,
        "launch_agent",
        lambda *a, **k: cursor_cloud.LaunchResult(ok=False, reason="submit-failed"),
    )
    rc = cli.main(
        [
            "run",
            "cloud",
            "launch",
            "--target",
            str(tmp_path),
            "--provider",
            "cursor-cloud",
            "--repo",
            "owner/repo",
            "--label",
            "failed-launch",
            "--prompt-file",
            str(prompt_file),
            "--json",
        ]
    )
    assert rc == 1
    out = capsys.readouterr()
    payload = json.loads(out.out)
    assert payload["ok"] is False
    assert payload["reason"] == "submit-failed"
    assert "do not persist this" not in out.out
    assert "do not persist this" not in out.err
    assert cloud_tracker.load_registry(tmp_path)["entries"] == []


def test_cli_run_cloud_launch_rejects_api_key_argv(tmp_path: Path):
    prompt_file = tmp_path / "prompt.txt"
    prompt_file.write_text("hi", encoding="utf-8")
    prompt_file.chmod(0o600)
    with pytest.raises(SystemExit):
        cli.main(
            [
                "run",
                "cloud",
                "launch",
                "--target",
                str(tmp_path),
                "--provider",
                "jules",
                "--repo",
                "owner/repo",
                "--label",
                "argv-key",
                "--prompt-file",
                str(prompt_file),
                "--api-key",
                "should-never-be-accepted",
            ]
        )
    assert cloud_tracker.load_registry(tmp_path)["entries"] == []


def test_cli_run_cloud_launch_cursor_tracking_failed_json_omits_secrets(tmp_path: Path, capsys, monkeypatch):
    prompt = "secret launch prompt text"
    prompt_file = tmp_path / "prompt.txt"
    prompt_file.write_text(prompt, encoding="utf-8")
    prompt_file.chmod(0o600)
    _isolate_hosted_providers(monkeypatch)
    monkeypatch.setenv("CURSOR_API_KEY", "cursor-api-key-deadbeef")
    monkeypatch.setattr(
        fleet_client,
        "admit_cloud",
        lambda *a, **k: fleet_client.CloudDecision(True, "ok", lease={"lease_id": "lease-1"}, holder="secret-holder"),
    )
    monkeypatch.setattr(fleet_client, "bind_cloud", lambda *a, **k: fleet_client.CloudDecision(True, "ok"))
    monkeypatch.setattr(fleet_client, "release_cloud", lambda *a, **k: fleet_client.CloudDecision(True, "ok"))
    monkeypatch.setattr(
        cursor_cloud,
        "_default_opener",
        lambda req, timeout=None: _cursor_create_response(),
    )
    monkeypatch.setattr(
        cloud_tracker,
        "register",
        lambda *a, **k: (_ for _ in ()).throw(OSError("disk full")),
    )
    rc = cli.main(
        [
            "run",
            "cloud",
            "launch",
            "--target",
            str(tmp_path),
            "--provider",
            "cursor-cloud",
            "--repo",
            "owner/repo",
            "--label",
            "cli-track",
            "--prompt-file",
            str(prompt_file),
            "--json",
        ]
    )
    assert rc != 0
    out = capsys.readouterr()
    payload = json.loads(out.out)
    assert payload["ok"] is False
    assert payload["reason"] == "tracking-failed"
    assert payload["task_id"] == "agent-live"
    assert payload["run_id"] == "run-live"
    assert payload["label"] == "cli-track"
    assert "holder" not in payload
    assert "lease_holder" not in payload
    assert "secret-holder" not in out.out
    assert "secret-holder" not in out.err
    assert "cursor-api-key-deadbeef" not in out.out
    assert "cursor-api-key-deadbeef" not in out.err
    assert prompt not in out.out
    assert prompt not in out.err
    assert cloud_tracker.load_registry(tmp_path)["entries"] == []


def test_cli_run_cloud_launch_jules_tracking_failed_json_omits_secrets(tmp_path: Path, capsys, monkeypatch):
    prompt = "secret jules prompt text"
    prompt_file = tmp_path / "prompt.txt"
    prompt_file.write_text(prompt, encoding="utf-8")
    prompt_file.chmod(0o600)
    monkeypatch.setenv("JULES_API_KEY", "jules-api-key-deadbeef")
    monkeypatch.setattr(
        fleet_client,
        "admit_cloud",
        lambda *a, **k: fleet_client.CloudDecision(True, "ok", lease={"lease_id": "lease-1"}, holder="secret-holder"),
    )
    monkeypatch.setattr(fleet_client, "bind_cloud", lambda *a, **k: fleet_client.CloudDecision(True, "ok"))
    monkeypatch.setattr(fleet_client, "release_cloud", lambda *a, **k: fleet_client.CloudDecision(True, "ok"))
    monkeypatch.setattr(jules_cloud, "_default_opener", _JulesLaunchOpener().open)
    monkeypatch.setattr(
        cloud_tracker,
        "register",
        lambda *a, **k: (_ for _ in ()).throw(OSError("disk full")),
    )
    rc = cli.main(
        [
            "run",
            "cloud",
            "launch",
            "--target",
            str(tmp_path),
            "--provider",
            "jules",
            "--repo",
            "owner/repo",
            "--label",
            "jules-track",
            "--prompt-file",
            str(prompt_file),
            "--json",
        ]
    )
    assert rc != 0
    out = capsys.readouterr()
    payload = json.loads(out.out)
    assert payload["ok"] is False
    assert payload["reason"] == "tracking-failed"
    assert payload["task_id"] == "sess-live"
    assert payload["session_id"] == "sess-live"
    assert "holder" not in payload
    assert "lease_holder" not in payload
    assert "secret-holder" not in out.out
    assert "jules-api-key-deadbeef" not in out.out
    assert prompt not in out.out
    assert prompt not in out.err
    assert cloud_tracker.load_registry(tmp_path)["entries"] == []


def test_cli_run_cloud_launch_prompt_file_symlink_rejected(tmp_path: Path, capsys, monkeypatch):
    real = tmp_path / "real.txt"
    real.write_text("symlink target prompt", encoding="utf-8")
    real.chmod(0o600)
    link = tmp_path / "prompt.txt"
    link.symlink_to(real)
    monkeypatch.setenv("CURSOR_API_KEY", "cursor-api-key-deadbeef")
    calls: list[dict[str, Any]] = []
    monkeypatch.setattr(
        cursor_cloud,
        "launch_agent",
        lambda *a, **k: (
            calls.append({"args": a, "kwargs": k}) or cursor_cloud.LaunchResult(ok=True, agent_id="nope", reason="ok")
        ),
    )
    rc = cli.main(
        [
            "run",
            "cloud",
            "launch",
            "--target",
            str(tmp_path),
            "--provider",
            "cursor-cloud",
            "--repo",
            "owner/repo",
            "--label",
            "symlink-file",
            "--prompt-file",
            str(link),
            "--json",
        ]
    )
    assert rc == 2
    payload = json.loads(capsys.readouterr().out)
    assert payload["ok"] is False
    assert payload["reason"] == "bad-prompt-file"
    assert calls == []


def test_cli_run_cloud_launch_prompt_file_group_writable_rejected(tmp_path: Path, capsys, monkeypatch):
    prompt_file = tmp_path / "prompt.txt"
    prompt_file.write_text("group writable prompt", encoding="utf-8")
    prompt_file.chmod(0o660)
    monkeypatch.setenv("CURSOR_API_KEY", "cursor-api-key-deadbeef")
    calls: list[dict[str, Any]] = []
    monkeypatch.setattr(
        cursor_cloud,
        "launch_agent",
        lambda *a, **k: (
            calls.append({"args": a, "kwargs": k}) or cursor_cloud.LaunchResult(ok=True, agent_id="nope", reason="ok")
        ),
    )
    rc = cli.main(
        [
            "run",
            "cloud",
            "launch",
            "--target",
            str(tmp_path),
            "--provider",
            "cursor-cloud",
            "--repo",
            "owner/repo",
            "--label",
            "unsafe-mode",
            "--prompt-file",
            str(prompt_file),
            "--json",
        ]
    )
    assert rc == 2
    payload = json.loads(capsys.readouterr().out)
    assert payload["ok"] is False
    assert payload["reason"] == "bad-prompt-file"
    assert calls == []


def test_read_prompt_file_path_swap_keeps_opened_snapshot(tmp_path: Path, monkeypatch):
    path = tmp_path / "prompt.txt"
    path.write_text("snapshot-one", encoding="utf-8")
    path.chmod(0o600)
    opened = {"n": 0, "flags": 0}
    real_open = os.open

    def open_and_replace(target, flags, *args, **kwargs):
        handle = real_open(target, flags, *args, **kwargs)
        if kwargs.get("dir_fd") is not None:
            return handle
        opened["n"] += 1
        opened["flags"] = flags
        decoy = tmp_path / "decoy.txt"
        decoy.write_text("snapshot-two", encoding="utf-8")
        decoy.chmod(0o600)
        os.replace(decoy, path)
        return handle

    monkeypatch.setattr(os, "open", open_and_replace)
    assert run_cloud._read_prompt_file(path) == "snapshot-one"
    assert opened["n"] >= 1
    nofollow = getattr(os, "O_NOFOLLOW", 0)
    if nofollow:
        assert opened["flags"] & nofollow


def test_read_prompt_file_fails_closed_before_a_forced_nofollow_fallback_race(tmp_path: Path, monkeypatch):
    path = tmp_path / "prompt.txt"
    path.write_text("snapshot-one", encoding="utf-8")
    path.chmod(0o600)
    decoy = tmp_path / "decoy.txt"
    decoy.write_text("snapshot-two", encoding="utf-8")
    decoy.chmod(0o600)
    opened = False
    real_open = os.open

    def raced_open(target, flags, *args, **kwargs):
        nonlocal opened
        opened = True
        os.replace(decoy, path)
        return real_open(target, flags, *args, **kwargs)

    monkeypatch.setattr(run_cloud.os, "O_NOFOLLOW", 0)
    monkeypatch.setattr(run_cloud.os, "open", raced_open)

    with pytest.raises(ValueError, match="bad-prompt-file"):
        run_cloud._read_prompt_file(path)

    assert opened is False
    assert path.read_text(encoding="utf-8") == "snapshot-one"


def test_cli_run_cloud_launch_label_canonicalized_in_json_and_registry(tmp_path: Path, capsys, monkeypatch):
    prompt_file = tmp_path / "prompt.txt"
    prompt_file.write_text("canonical label prompt", encoding="utf-8")
    prompt_file.chmod(0o600)
    _isolate_hosted_providers(monkeypatch)
    monkeypatch.setenv("CURSOR_API_KEY", "cursor-api-key-deadbeef")
    captured: dict[str, Any] = {}

    def fake_launch(api_key: str, **kwargs: Any):
        captured.update(kwargs)
        cloud_tracker.register(
            kwargs["register_target"],
            provider="cursor-cloud",
            task_id="agent-label",
            label=kwargs["label"],
            prompt_hash=cloud_tracker.prompt_hash(kwargs["prompt"]),
            expected_artifact={"kind": "diff"},
            lease_holder="private-holder",
        )
        return cursor_cloud.LaunchResult(ok=True, agent_id="agent-label", run_id="run-label", reason="ok")

    monkeypatch.setattr(cursor_cloud, "launch_agent", fake_launch)
    rc = cli.main(
        [
            "run",
            "cloud",
            "launch",
            "--target",
            str(tmp_path),
            "--provider",
            "cursor-cloud",
            "--repo",
            "owner/repo",
            "--label",
            "  padded-label  ",
            "--prompt-file",
            str(prompt_file),
            "--json",
        ]
    )
    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["label"] == "padded-label"
    assert captured["label"] == "padded-label"
    assert cloud_tracker.load_registry(tmp_path)["entries"][0]["label"] == "padded-label"


@pytest.mark.parametrize(
    "label",
    ["", "   ", "x" * 121, "ok\x00bad", "ok\nbad"],
)
def test_cli_run_cloud_launch_invalid_label_makes_zero_provider_calls(tmp_path: Path, capsys, monkeypatch, label: str):
    prompt_file = tmp_path / "prompt.txt"
    prompt_file.write_text("unused prompt", encoding="utf-8")
    prompt_file.chmod(0o600)
    monkeypatch.setenv("CURSOR_API_KEY", "cursor-api-key-deadbeef")
    calls: list[dict[str, Any]] = []
    monkeypatch.setattr(
        cursor_cloud,
        "launch_agent",
        lambda *a, **k: (
            calls.append({"args": a, "kwargs": k}) or cursor_cloud.LaunchResult(ok=True, agent_id="nope", reason="ok")
        ),
    )
    rc = cli.main(
        [
            "run",
            "cloud",
            "launch",
            "--target",
            str(tmp_path),
            "--provider",
            "cursor-cloud",
            "--repo",
            "owner/repo",
            "--label",
            label,
            "--prompt-file",
            str(prompt_file),
            "--json",
        ]
    )
    assert rc == 2
    payload = json.loads(capsys.readouterr().out)
    assert payload["ok"] is False
    assert payload["reason"] == "bad-label"
    assert calls == []
    assert cloud_tracker.load_registry(tmp_path)["entries"] == []


def test_cli_run_cloud_launch_cursor_rejects_starting_branch_before_mutation(tmp_path: Path, capsys, monkeypatch):
    prompt_file = tmp_path / "prompt.txt"
    prompt_file.write_text("unused prompt", encoding="utf-8")
    prompt_file.chmod(0o600)
    monkeypatch.setenv("CURSOR_API_KEY", "cursor-api-key-deadbeef")
    calls: list[dict[str, Any]] = []
    monkeypatch.setattr(
        cursor_cloud,
        "launch_agent",
        lambda *a, **k: (
            calls.append({"args": a, "kwargs": k}) or cursor_cloud.LaunchResult(ok=True, agent_id="nope", reason="ok")
        ),
    )
    rc = cli.main(
        [
            "run",
            "cloud",
            "launch",
            "--target",
            str(tmp_path),
            "--provider",
            "cursor-cloud",
            "--repo",
            "owner/repo",
            "--label",
            "cursor-flags",
            "--prompt-file",
            str(prompt_file),
            "--starting-branch",
            "dev",
            "--json",
        ]
    )
    assert rc == 2
    payload = json.loads(capsys.readouterr().out)
    assert payload["ok"] is False
    assert payload["reason"] == "unsupported-flag"
    assert calls == []


def test_cli_run_cloud_launch_cursor_rejects_title_before_mutation(tmp_path: Path, capsys, monkeypatch):
    prompt_file = tmp_path / "prompt.txt"
    prompt_file.write_text("unused prompt", encoding="utf-8")
    prompt_file.chmod(0o600)
    monkeypatch.setenv("CURSOR_API_KEY", "cursor-api-key-deadbeef")
    calls: list[dict[str, Any]] = []
    monkeypatch.setattr(
        cursor_cloud,
        "launch_agent",
        lambda *a, **k: (
            calls.append({"args": a, "kwargs": k}) or cursor_cloud.LaunchResult(ok=True, agent_id="nope", reason="ok")
        ),
    )
    rc = cli.main(
        [
            "run",
            "cloud",
            "launch",
            "--target",
            str(tmp_path),
            "--provider",
            "cursor-cloud",
            "--repo",
            "owner/repo",
            "--label",
            "cursor-flags",
            "--prompt-file",
            str(prompt_file),
            "--title",
            "feature work",
            "--json",
        ]
    )
    assert rc == 2
    payload = json.loads(capsys.readouterr().out)
    assert payload["ok"] is False
    assert payload["reason"] == "unsupported-flag"
    assert calls == []


def _cursor_create_response():
    from unittest.mock import MagicMock

    response = MagicMock()
    response.status = 200
    response.__enter__ = MagicMock(return_value=response)
    response.__exit__ = MagicMock(return_value=False)
    response.read.return_value = json.dumps(
        {
            "agent": {"id": "agent-live", "latestRunId": "run-live"},
            "run": {"id": "run-live", "status": "CREATING"},
        }
    ).encode("utf-8")
    return response


class _JulesLaunchOpener:
    def __init__(self) -> None:
        from unittest.mock import MagicMock

        sources = MagicMock()
        sources.status = 200
        sources.__enter__ = MagicMock(return_value=sources)
        sources.__exit__ = MagicMock(return_value=False)
        sources.read.return_value = json.dumps(
            {
                "sources": [
                    {
                        "name": "sources/github-owner-repo",
                        "id": "github-owner-repo",
                        "githubRepo": {
                            "owner": "owner",
                            "repo": "repo",
                            "branches": [{"displayName": "main"}],
                            "defaultBranch": {"displayName": "main"},
                        },
                    }
                ],
                "nextPageToken": None,
            }
        ).encode("utf-8")
        created = MagicMock()
        created.status = 200
        created.__enter__ = MagicMock(return_value=created)
        created.__exit__ = MagicMock(return_value=False)
        created.read.return_value = json.dumps({"id": "sess-live", "state": "QUEUED"}).encode("utf-8")
        self._responses = [sources, created]

    def open(self, req, timeout=None):
        return self._responses.pop(0)


def test_cli_run_cloud_compact_json_contract(tmp_path: Path, capsys, monkeypatch):
    ids = _seed_mixed_registry(tmp_path)
    provider_tasks, github = _mixed_provider_and_github()
    observations = {
        provider: cloud_tracker.ProviderObservation(False, False, "unconfigured", {})
        for provider in cloud_tracker.TRACKER_PROVIDERS
    }
    observations["codex-cloud"] = cloud_tracker.ProviderObservation(
        True,
        True,
        None,
        provider_tasks,
    )
    monkeypatch.setattr(
        cloud_tracker,
        "observe_provider_details",
        lambda target, **kwargs: (observations, github),
    )
    rc = cli.main(
        [
            "run",
            "cloud",
            "compact",
            "--target",
            str(tmp_path),
            "--keep-terminal",
            "1",
            "--max-age-hours",
            "24",
            "--json",
        ]
    )
    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["schema"] == cloud_tracker.MAINTENANCE_SCHEMA
    assert payload["action"] == "compact"
    assert payload["policy"]["keep_terminal"] == 1
    kept_ids = {entry["id"] for entry in cloud_tracker.load_registry(tmp_path)["entries"]}
    assert ids["orphaned"] in kept_ids
    assert ids["needs"] in kept_ids
    assert ids["landed-old"] not in kept_ids
