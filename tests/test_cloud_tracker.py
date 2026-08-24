"""Contract tests for the cloud dispatch registry (#890)."""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from brigade import cli, cloud_tracker, grokbot_jobs


NOW = datetime(2026, 8, 12, 20, 0, 0, tzinfo=timezone.utc)


def _iso(dt: datetime) -> str:
    return dt.isoformat().replace("+00:00", "Z")


def _prompt_hash(text: str) -> str:
    return "sha256:" + hashlib.sha256(text.encode()).hexdigest()


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
