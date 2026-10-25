"""Read-only lane classification and the patrol command contract."""

from __future__ import annotations

import json
import os
from datetime import timedelta

import pytest

from brigade import cli, runguard
from tests.test_run_reap import NOW, OLD_STARTED, _repo, _write_run, _write_stale_lock


def _snapshot(root):
    return {
        str(path.relative_to(root)): (path.stat().st_mtime_ns, path.read_bytes() if path.is_file() else None)
        for path in root.rglob("*")
        if not path.is_symlink()
    }


def test_default_patrol_is_read_only(tmp_path, capsys, monkeypatch):
    repo = _repo(tmp_path)
    run_dir = _write_run(repo, "20260820-120000-patrol01")
    _write_stale_lock(repo, run_dir)
    ledger = repo / ".brigade/work/tasks.json"
    ledger.parent.mkdir()
    ledger.write_text(json.dumps({"tasks": []}))
    monkeypatch.setattr(runguard, "_pid_is_active", lambda pid: False)
    before = _snapshot(repo)
    assert cli.main(["runs", "patrol", "--target", str(repo), "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["schema"] == "brigade.runs_patrol.v1"
    assert payload["lanes"][0]["state"] == "dead"
    assert payload["actions"] == []
    assert payload["lanes"][0]["intent"] is None
    assert all(item["source"] and item["observed_at"] for item in payload["lanes"][0]["evidence"])
    assert _snapshot(repo) == before


@pytest.mark.parametrize("elapsed", [5, 120])
def test_patrol_uses_clock_after_collection(tmp_path, capsys, monkeypatch, elapsed):
    from brigade import run_reap

    repo = _repo(tmp_path)
    later = NOW + timedelta(seconds=elapsed)
    run_dir = _write_run(repo, "20260820-120000-patrol01", started_at=later.isoformat())
    _write_stale_lock(repo, run_dir)
    monkeypatch.setattr(runguard, "_pid_is_active", lambda pid: True)
    signal = run_dir / "events/worker.jsonl"
    signal.parent.mkdir()
    signal.write_text("{}\n")
    os.utime(signal, (later.timestamp(), later.timestamp()))
    clocks = iter((NOW, later))
    monkeypatch.setattr(run_reap, "_utc_now", lambda now=None: now if now is not None else next(clocks))
    assert cli.main(["runs", "patrol", "--target", str(repo), "--json", "--fail-on", "unknown"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["observed_at"] == later.isoformat()
    assert payload["lanes"][0]["state"] == "working"
    assert payload["scan_duration_seconds"] == elapsed


@pytest.mark.parametrize(
    ("status", "live", "progress_age", "extra", "expected"),
    [
        ("ok", False, None, {"finished_at": OLD_STARTED}, "complete"),
        ("failed", False, None, {"finished_at": OLD_STARTED}, "failed"),
        ("orphaned", False, None, {"finished_at": OLD_STARTED}, "orphaned"),
        ("dispatching", False, None, {}, "dead"),
        ("dispatching", True, 60, {}, "working"),
        ("dispatching", True, 1000, {}, "idle"),
        ("dispatching", True, 2000, {}, "stalled"),
        ("running", True, 4000, {"approval_reference": {"decision_state": "pending"}}, "waiting"),
        ("artifact-collection", True, 4000, {}, "waiting"),
        ("dispatching", True, None, {}, "unknown"),
    ],
)
def test_lane_states(tmp_path, monkeypatch, status, live, progress_age, extra, expected):
    from brigade.run_lane_state import Thresholds, classify, collect_lanes

    repo = _repo(tmp_path)
    run_dir = _write_run(repo, "20260820-120000-patrol01", status=status, extra=extra)
    if status not in {"ok", "failed"}:
        _write_stale_lock(repo, run_dir)
    monkeypatch.setattr(runguard, "_pid_is_active", lambda pid: live)
    if progress_age is not None:
        events = run_dir / "events"
        events.mkdir()
        signal = events / "worker.jsonl"
        signal.write_text("{}\n")
        stamp = (NOW - timedelta(seconds=progress_age)).timestamp()
        os.utime(signal, (stamp, stamp))
    lanes = collect_lanes(repo, now=NOW)
    result = classify(lanes[0], NOW, Thresholds())
    assert result.state == expected
    assert result.evidence


@pytest.mark.parametrize(
    "status,extra",
    [("artifact-collection", {}), ("running", {"approval_reference": {"decision_state": "pending"}}), ("paused", {})],
)
@pytest.mark.parametrize("lock_present", [True, False])
def test_dead_owner_precedes_waiting(tmp_path, capsys, monkeypatch, status, extra, lock_present):
    from brigade.runs_patrol import patrol

    repo = _repo(tmp_path)
    run_dir = _write_run(repo, "20260820-120000-patrol01", status=status, extra=extra)
    if lock_present:
        _write_stale_lock(repo, run_dir)
    monkeypatch.setattr(runguard, "_pid_is_active", lambda pid: False)
    assert patrol(target=repo, now=NOW, json_output=True, fail_on="dead") == int(lock_present)
    assert json.loads(capsys.readouterr().out)["lanes"][0]["state"] == ("dead" if lock_present else "waiting")


def test_corrupt_journal_is_unknown_even_with_live_progress(tmp_path, monkeypatch):
    from brigade.run_lane_state import Thresholds, classify, collect_lanes

    repo = _repo(tmp_path)
    run_dir = _write_run(repo, "20260820-120000-patrol01", extra={"lifecycle_journal_requested": True})
    _write_stale_lock(repo, run_dir)
    monkeypatch.setattr(runguard, "_pid_is_active", lambda pid: True)
    events = run_dir / "events"
    events.mkdir()
    (events / "lifecycle.jsonl").write_text("corrupt\n")
    assert classify(collect_lanes(repo, now=NOW)[0], NOW, Thresholds()).state == "unknown"


@pytest.mark.parametrize(
    ("live", "finished_age", "expected"), [(True, 61, "zombie"), (True, 30, "complete"), (False, 61, "complete")]
)
def test_terminal_owner_grace(tmp_path, monkeypatch, live, finished_age, expected):
    from brigade.run_lane_state import Thresholds, classify, collect_lanes

    repo = _repo(tmp_path)
    finished = (NOW - timedelta(seconds=finished_age)).isoformat()
    run_dir = _write_run(repo, "20260820-120000-patrol01", status="ok", extra={"finished_at": finished})
    if live:
        _write_stale_lock(repo, run_dir)
    monkeypatch.setattr(runguard, "_pid_is_active", lambda pid: live)
    assert classify(collect_lanes(repo, now=NOW)[0], NOW, Thresholds()).state == expected


def _ledger(repo, *, worktree=None):
    task = {
        "id": "task-1",
        "status": "in_progress",
        "claim": {"claim_id": "claim-1", "actor": "worker", "claimed_at": OLD_STARTED},
    }
    if worktree is not None:
        task["worktree"] = str(worktree)
    path = repo / ".brigade/work/tasks.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"tasks": [task]}))
    return path


@pytest.mark.parametrize("status", ["done", "cancelled"])
def test_finished_tasks_with_retained_claims_do_not_consume_claim_window(tmp_path, status):
    from brigade.run_lane_state import collect_lanes

    repo = _repo(tmp_path)
    ledger = _ledger(repo)
    active = json.loads(ledger.read_text())["tasks"][0]
    finished = [
        {
            **active,
            "id": f"finished-{index}",
            "status": status,
            "completed_at": OLD_STARTED,
            "claim": {**active["claim"], "claim_id": f"finished-claim-{index}"},
        }
        for index in range(60)
    ]
    ledger.write_text(json.dumps({"tasks": [active, *finished]}))
    lanes = collect_lanes(repo, now=NOW, limit=1)
    assert [lane.task["id"] for lane in lanes] == ["task-1"]
    assert not lanes.truncated


@pytest.mark.parametrize(
    ("kind", "claim_minutes", "expected"),
    [
        ("claim", 30, "unknown"),
        ("claim", 90, "unknown"),
        ("dirty", None, "stranded"),
        ("missing", None, "lost-worktree"),
    ],
)
def test_claim_and_worktree_lanes(tmp_path, kind, claim_minutes, expected):
    from brigade.run_lane_state import Thresholds, classify, collect_lanes

    repo = _repo(tmp_path)
    ledger = _ledger(repo, worktree=repo / "gone" if kind == "missing" else repo if kind == "dirty" else None)
    if claim_minutes is not None:
        payload = json.loads(ledger.read_text())
        payload["tasks"][0]["claim"]["claimed_at"] = (NOW - timedelta(minutes=claim_minutes)).isoformat()
        ledger.write_text(json.dumps(payload))
    if kind == "dirty":
        (repo / "tracked.txt").write_text("unlanded work\n")
        stamp = (NOW - timedelta(days=2)).timestamp()
        os.utime(repo / "tracked.txt", (stamp, stamp))
    lanes = collect_lanes(repo, now=NOW)
    assert len(lanes) == 1
    result = classify(lanes[0], NOW, Thresholds())
    assert result.state == expected
    if kind == "claim":
        assert "no task-to-run join key" in result.reason


def test_recorded_missing_run_worktree(tmp_path, monkeypatch):
    from brigade.run_lane_state import Thresholds, classify, collect_lanes

    repo = _repo(tmp_path)
    run_dir = _write_run(repo, "20260820-120000-patrol01", extra={"worktree": str(repo / "gone")})
    _write_stale_lock(repo, run_dir)
    monkeypatch.setattr(runguard, "_pid_is_active", lambda pid: True)
    assert classify(collect_lanes(repo, now=NOW)[0], NOW, Thresholds()).state == "lost-worktree"


def test_receipt_and_fail_on(tmp_path, capsys, monkeypatch):
    from brigade.runs_patrol import patrol

    repo = _repo(tmp_path)
    run_dir = _write_run(repo, "20260820-120000-patrol01")
    _write_stale_lock(repo, run_dir)
    monkeypatch.setattr(runguard, "_pid_is_active", lambda pid: False)
    before = _snapshot(repo / ".brigade/runs")
    assert patrol(target=repo, now=NOW, receipt=True, json_output=True, fail_on="dead,stalled") == 1
    payload = json.loads(capsys.readouterr().out)
    stored = json.loads((repo / payload["receipt"]).read_text())
    assert stored == payload
    assert stored["thresholds"]["idle_after"] == 900
    assert stored["exit_code"] == 1
    assert _snapshot(repo / ".brigade/runs") == before


@pytest.mark.parametrize(
    "options", [["--limit", "0"], ["--idle-after", "0s"], ["--stall-after", "1m"], ["--fail-on", "nonsense"]]
)
def test_usage_errors(tmp_path, capsys, options):
    assert cli.main(["runs", "patrol", "--target", str(tmp_path), *options]) == 2
    assert "error:" in capsys.readouterr().err
    assert not (tmp_path / ".brigade").exists()


def test_scan_is_bounded_and_skips_symlinked_and_malformed_dirs(tmp_path, monkeypatch):
    from brigade.run_lane_state import collect_lanes

    repo = _repo(tmp_path)
    run_dir = _write_run(repo, "20260820-120000-patrol01")
    _write_run(repo, "20260820-120000-patrol02")
    root = run_dir.parent
    (root / "bad name").mkdir()
    (root / "20260820-120000-linked01").symlink_to(run_dir, target_is_directory=True)
    monkeypatch.setattr(runguard, "_pid_is_active", lambda pid: False)
    assert len(collect_lanes(repo, now=NOW, limit=1)) <= 1
    assert {lane.lane_id for lane in collect_lanes(repo, now=NOW)} == {
        "20260820-120000-patrol01",
        "20260820-120000-patrol02",
    }


@pytest.mark.parametrize(
    "bad_input", ["run", "owner", "journal-missing", "ledger", "oversized", "owner-foreign", "owner-symlink"]
)
def test_unverifiable_evidence_is_unknown(tmp_path, monkeypatch, bad_input):
    from brigade.run_lane_state import FILE_BYTE_CAP, Thresholds, classify, collect_lanes

    repo = _repo(tmp_path)
    run_dir = _write_run(repo, "20260820-120000-patrol01")
    lock = _write_stale_lock(repo, run_dir)
    monkeypatch.setattr(runguard, "_pid_is_active", lambda pid: True)
    if bad_input == "run":
        (run_dir / "run.json").write_text("corrupt")
    elif bad_input == "owner":
        (lock / "owner.json").write_text("corrupt")
    elif bad_input == "owner-foreign":
        owner = json.loads((lock / "owner.json").read_text())
        owner["run_dir"] = str(repo / "other")
        (lock / "owner.json").write_text(json.dumps(owner))
    elif bad_input == "owner-symlink":
        (lock / "owner.json").rename(lock / "other.json")
        (lock / "owner.json").symlink_to(lock / "other.json")
    elif bad_input == "journal-missing":
        meta = json.loads((run_dir / "run.json").read_text())
        meta["lifecycle_journal_requested"] = True
        (run_dir / "run.json").write_text(json.dumps(meta))
    elif bad_input == "ledger":
        _ledger(repo).write_text("corrupt")
    else:
        (run_dir / "run.json").write_bytes(b"x" * (FILE_BYTE_CAP + 1))
    assert classify(collect_lanes(repo, now=NOW)[0], NOW, Thresholds()).state == "unknown"


def test_timeout_uses_injected_clock_and_overrides_recent_progress(tmp_path, monkeypatch):
    from brigade.run_lane_state import Thresholds, classify, collect_lanes

    repo = _repo(tmp_path)
    run_dir = _write_run(
        repo,
        "20260820-120000-patrol01",
        extra={"worker": "worker", "status_started_at": (NOW - timedelta(seconds=61)).isoformat()},
    )
    _write_stale_lock(repo, run_dir)
    monkeypatch.setattr(runguard, "_pid_is_active", lambda pid: True)
    (run_dir / "roster.json").write_text(json.dumps({"agents": {"worker": {"timeout_seconds": 60}}}))
    events = run_dir / "events"
    events.mkdir()
    signal = events / "worker.jsonl"
    signal.write_text("{}\n")
    os.utime(signal, (NOW.timestamp(), NOW.timestamp()))
    lane = collect_lanes(repo, now=NOW)[0]
    assert classify(lane, NOW, Thresholds()).state == "stalled"
    assert classify(lane, NOW + timedelta(minutes=2), Thresholds()).state == "unknown"


def test_unchanged_evidence_keeps_classification_digest(tmp_path, capsys, monkeypatch):
    from brigade.runs_patrol import patrol

    repo = _repo(tmp_path)
    run_dir = _write_run(repo, "20260820-120000-patrol01")
    _write_stale_lock(repo, run_dir)
    monkeypatch.setattr(runguard, "_pid_is_active", lambda pid: False)
    assert patrol(target=repo, now=NOW, json_output=True) == 0
    first = json.loads(capsys.readouterr().out)
    assert patrol(target=repo, now=NOW + timedelta(seconds=1), json_output=True) == 0
    second = json.loads(capsys.readouterr().out)
    assert first["lanes"][0]["classification_digest"] == second["lanes"][0]["classification_digest"]


@pytest.mark.parametrize(
    ("event_type", "payload", "expected"),
    [
        ("run.completed", {"status": "ok"}, "complete"),
        ("run.paused", {"approval_id": "approval-1", "reason": "pending"}, "waiting"),
        ("run.artifact_collection.started", {"detail": "collecting worker artifacts"}, "waiting"),
    ],
)
def test_journal_wins_over_stale_snapshot(tmp_path, monkeypatch, event_type, payload, expected):
    from brigade import run_journal
    from brigade.run_lane_state import Thresholds, classify, collect_lanes

    repo = _repo(tmp_path)
    run_dir = _write_run(
        repo, "20260820-120000-patrol01", status="running", extra={"lifecycle_journal_requested": True}
    )
    if expected == "waiting":
        _write_stale_lock(repo, run_dir)
    monkeypatch.setattr(runguard, "_pid_is_active", lambda pid: True)
    run_journal.append_event(
        run_dir / "events/lifecycle.jsonl",
        run_id=run_dir.name,
        event_type=event_type,
        payload=payload,
        idempotency_key="event-1",
        expected_previous_sequence=0,
        recorded_at="2026-08-28T17:59:00.000000Z",
    )
    assert classify(collect_lanes(repo, now=NOW)[0], NOW, Thresholds()).state == expected


@pytest.mark.parametrize(
    "event_type,payload",
    [("run.recovery.started", {"detail": "research reopened"}), ("run.resumed", {"approval_id": "approval-1"})],
)
def test_journal_reopen_clears_prior_terminal_state(tmp_path, monkeypatch, event_type, payload):
    from brigade import run_journal
    from brigade.run_lane_state import Thresholds, classify, collect_lanes

    repo = _repo(tmp_path)
    run_dir = _write_run(repo, "20260820-120000-patrol01", status="running", extra={"kind": "research"})
    _write_stale_lock(repo, run_dir)
    monkeypatch.setattr(runguard, "_pid_is_active", lambda pid: True)
    for sequence, (kind, data, timestamp) in enumerate(
        [
            ("run.completed", {"status": "completed"}, "2026-08-20T12:00:00.000000Z"),
            (event_type, payload, "2026-08-28T17:59:50.000000Z"),
        ]
    ):
        run_journal.append_event(
            run_dir / "events/lifecycle.jsonl",
            run_id=run_dir.name,
            event_type=kind,
            payload=data,
            idempotency_key=f"event-{sequence}",
            expected_previous_sequence=sequence,
            recorded_at=timestamp,
        )
    assert classify(collect_lanes(repo, now=NOW)[0], NOW, Thresholds()).state == "working"


@pytest.mark.parametrize("path_count", [1, 201])
def test_worktree_changes_supply_progress_without_event_files(tmp_path, monkeypatch, path_count):
    from brigade.run_lane_state import Thresholds, classify, collect_lanes

    repo = _repo(tmp_path)
    run_dir = _write_run(repo, "20260820-120000-patrol01")
    _write_stale_lock(repo, run_dir)
    monkeypatch.setattr(runguard, "_pid_is_active", lambda pid: True)
    tracked = repo / "tracked.txt"
    tracked.write_text("progress\n")
    os.utime(tracked, (NOW.timestamp() - 10, NOW.timestamp() - 10))
    for index in range(path_count - 1):
        changed = repo / f"changed-{index:03d}"
        changed.write_text("progress\n")
        os.utime(changed, (NOW.timestamp() - 10, NOW.timestamp() - 10))
    lane = collect_lanes(repo, now=NOW)[0]
    assert classify(lane, NOW, Thresholds()).state == "working"
    assert any(
        item.source == "worktree:git-status" and item.detail == f"{min(path_count, 200)} changed paths"
        for item in lane.evidence
    )


@pytest.mark.parametrize(
    "workspace,signal",
    [
        ("subdirectory", "events"),
        ("subdirectory", "changes"),
        ("linked-subdirectory", "events"),
        ("linked-subdirectory", "changes"),
        ("non-git", "events"),
    ],
)
def test_patrol_accepts_supported_workspaces(tmp_path, capsys, monkeypatch, workspace, signal):
    from brigade import proc, run_reap

    if workspace == "non-git":
        repo = tmp_path / "repo"
        repo.mkdir()
        cwd = repo
    else:
        repo = _repo(tmp_path)
        root = repo
        if workspace == "linked-subdirectory":
            root = tmp_path / "linked"
            result = proc.run(["git", "worktree", "add", "--detach", str(root)], cwd=repo)
            assert result.code == 0, result.stderr
        cwd = root / "subdir"
        cwd.mkdir()
    run_dir = _write_run(repo, "20260820-120000-patrol01", extra={"cwd": str(cwd)})
    _write_stale_lock(repo, run_dir)
    monkeypatch.setattr(runguard, "_pid_is_active", lambda pid: True)
    monkeypatch.setattr(run_reap, "_utc_now", lambda now=None: NOW)
    if signal == "events":
        (run_dir / "events").mkdir()
        progress = run_dir / "events/worker.jsonl"
    else:
        progress = cwd / "changed.txt"
    progress.write_text("{}\n")
    os.utime(progress, (NOW.timestamp() - 10, NOW.timestamp() - 10))
    before = _snapshot(repo)
    assert cli.main(["runs", "patrol", "--target", str(repo), "--json", "--fail-on", "unknown"]) == 0
    row = json.loads(capsys.readouterr().out)["lanes"][0]
    assert row["state"] == "working", row["reason"]
    if signal == "changes":
        assert any(item["source"] == "worktree:git-status" and item["timestamp"] for item in row["evidence"])
    assert _snapshot(repo) == before


@pytest.mark.parametrize("workspace", ["unrelated-git", "unrelated-non-git", "inventory-error", "invalid-git"])
def test_patrol_keeps_unverified_workspaces_unknown(tmp_path, monkeypatch, workspace):
    from brigade import proc, run_lane_state

    repo = _repo(tmp_path)
    cwd = repo
    if workspace.startswith("unrelated"):
        cwd = tmp_path / "repo-other"
        cwd.mkdir()
        if workspace == "unrelated-git":
            assert proc.run(["git", "init"], cwd=cwd).code == 0
    elif workspace == "invalid-git":
        repo = tmp_path / "broken"
        repo.mkdir()
        (repo / ".git").write_text("invalid gitfile\n")
        cwd = repo
    else:
        real_git = run_lane_state._git

        def git(path, *args):
            if args[:2] == ("worktree", "list"):
                return proc.Result(1, "", "inventory unavailable")
            return real_git(path, *args)

        monkeypatch.setattr(run_lane_state, "_git", git)
    run_dir = _write_run(repo, "20260820-120000-patrol01", extra={"cwd": str(cwd)})
    _write_stale_lock(repo, run_dir)
    monkeypatch.setattr(runguard, "_pid_is_active", lambda pid: True)
    (run_dir / "events").mkdir()
    progress = run_dir / "events/worker.jsonl"
    progress.write_text("{}\n")
    os.utime(progress, (NOW.timestamp() - 10, NOW.timestamp() - 10))
    lane = run_lane_state.collect_lanes(repo, now=NOW)[0]
    assert run_lane_state.classify(lane, NOW, run_lane_state.Thresholds()).state == "unknown"


@pytest.mark.parametrize("worktree", ["non-git", "subdirectory", "unreadable", "git-error"])
@pytest.mark.parametrize("live,expected", [(False, "dead"), (True, "stalled")])
def test_worktree_problems_do_not_mask_lock_or_timeout(tmp_path, monkeypatch, worktree, live, expected):
    from brigade import proc, run_lane_state

    if worktree == "non-git":
        repo = tmp_path / "repo"
        repo.mkdir()
    else:
        repo = _repo(tmp_path)
    cwd = repo / "subdir" if worktree == "subdirectory" else repo
    cwd.mkdir(exist_ok=True)
    run_dir = _write_run(repo, "20260820-120000-patrol01", extra={"cwd": str(cwd)})
    _write_stale_lock(repo, run_dir)
    (run_dir / "roster.json").write_text(json.dumps({"timeout_seconds": 60}))
    monkeypatch.setattr(runguard, "_pid_is_active", lambda pid: live)
    real_git = run_lane_state._git

    def git(path, *args):
        if worktree == "git-error" and args[0] == "status":
            raise OSError("worktree status unavailable")
        if worktree == "unreadable" and args[0] == "status":
            return proc.Result(1, "", "status unavailable")
        return real_git(path, *args)

    monkeypatch.setattr(run_lane_state, "_git", git)
    lane = run_lane_state.collect_lanes(repo, now=NOW)[0]
    result = run_lane_state.classify(lane, NOW, run_lane_state.Thresholds())
    assert result.state == expected


def test_claim_joins_run_by_claim_id(tmp_path, monkeypatch):
    from brigade.run_lane_state import Thresholds, classify, collect_lanes

    repo = _repo(tmp_path)
    _ledger(repo)
    run_dir = _write_run(repo, "20260820-120000-patrol01", extra={"claim_id": "claim-1"})
    _write_stale_lock(repo, run_dir)
    monkeypatch.setattr(runguard, "_pid_is_active", lambda pid: False)
    lanes = collect_lanes(repo, now=NOW)
    assert len(lanes) == 1
    assert lanes[0].claim["claim_id"] == "claim-1"
    assert classify(lanes[0], NOW, Thresholds()).state == "dead"


def test_claim_is_unknown_when_run_scan_is_incomplete(tmp_path, monkeypatch):
    from brigade import run_lane_state

    repo = _repo(tmp_path)
    _ledger(repo)
    for index in range(2):
        _write_run(repo, f"20260820-120000-patrol{index:02d}", status="ok")
    monkeypatch.setattr(run_lane_state, "RUN_SCAN_LIMIT", 1, raising=False)
    lanes = run_lane_state.collect_lanes(repo, now=NOW)
    claim = next(lane for lane in lanes if lane.claim)
    assert run_lane_state.classify(claim, NOW, run_lane_state.Thresholds()).state == "unknown"
    assert lanes.truncated


def test_receipt_refuses_symlinked_destination(tmp_path, capsys):
    from brigade.runs_patrol import patrol

    repo = _repo(tmp_path)
    outside = tmp_path / "outside"
    outside.mkdir()
    (repo / ".brigade").mkdir()
    (repo / ".brigade/patrol").symlink_to(outside, target_is_directory=True)
    assert patrol(target=repo, receipt=True, now=NOW) == 2
    assert "symlinked" in capsys.readouterr().err
    assert list(outside.iterdir()) == []


def test_missing_target_and_explicit_apply(tmp_path, capsys):
    assert cli.main(["runs", "patrol", "--target", str(tmp_path / "missing")]) == 2
    capsys.readouterr()
    assert cli.main(["runs", "patrol", "--target", str(tmp_path), "--apply", "reap"]) == 0
    assert list((tmp_path / ".brigade/patrol").glob("*/patrol.json"))


@pytest.mark.parametrize("status,expected", [("ok", "complete"), ("failed", "failed")])
def test_terminal_stale_lock_keeps_outcome_and_cleanup_evidence(tmp_path, monkeypatch, status, expected):
    from brigade.run_lane_state import Thresholds, classify, collect_lanes

    repo = _repo(tmp_path)
    run_dir = _write_run(repo, "20260820-120000-patrol01", status=status, extra={"finished_at": OLD_STARTED})
    _write_stale_lock(repo, run_dir)
    monkeypatch.setattr(runguard, "_pid_is_active", lambda pid: False)
    result = classify(collect_lanes(repo, now=NOW)[0], NOW, Thresholds())
    assert result.state == expected
    assert any(item.source == "run.lock/owner.json" and item.detail == "stale" for item in result.evidence)


def test_terminal_run_does_not_own_newer_runs_lock(tmp_path, monkeypatch):
    from brigade.run_lane_state import Thresholds, classify, collect_lanes

    repo = _repo(tmp_path)
    _write_run(repo, "20260820-120000-patrol01", status="ok", extra={"finished_at": OLD_STARTED})
    newer = _write_run(repo, "20260820-120000-patrol02")
    _write_stale_lock(repo, newer)
    monkeypatch.setattr(runguard, "_pid_is_active", lambda pid: True)
    lanes = collect_lanes(repo, now=NOW)
    first = next(lane for lane in lanes if lane.lane_id.endswith("patrol01"))
    assert classify(first, NOW, Thresholds()).state == "complete"


def test_failed_contract_includes_infrastructure_flag(tmp_path, capsys):
    from brigade.runs_patrol import patrol

    repo = _repo(tmp_path)
    _write_run(
        repo,
        "20260820-120000-patrol01",
        status="failed",
        extra={"failure": {"kind": "transport-error", "phase": "dispatch"}},
    )
    assert patrol(target=repo, now=NOW, json_output=True) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["lanes"][0]["state"] == "failed"
    assert payload["lanes"][0]["infrastructure_failure"] is True


@pytest.mark.parametrize("source", ["ledger-id", "dangling-events"])
def test_malformed_join_sources_never_report_healthy(tmp_path, monkeypatch, source):
    from brigade.run_lane_state import Thresholds, classify, collect_lanes

    repo = _repo(tmp_path)
    run_dir = _write_run(repo, "20260820-120000-patrol01", status="ok", extra={"finished_at": OLD_STARTED})
    if source == "ledger-id":
        ledger = _ledger(repo)
        payload = json.loads(ledger.read_text())
        del payload["tasks"][0]["id"]
        ledger.write_text(json.dumps(payload))
    else:
        (run_dir / "events").symlink_to(repo / "missing", target_is_directory=True)
    assert classify(collect_lanes(repo, now=NOW)[0], NOW, Thresholds()).state == "unknown"


def test_corrupt_source_is_named_in_evidence(tmp_path):
    from brigade.run_lane_state import collect_lanes

    repo = _repo(tmp_path)
    run_dir = _write_run(repo, "20260820-120000-patrol01")
    (run_dir / "events").mkdir()
    (run_dir / "events/lifecycle.jsonl").write_text("corrupt\n")
    lane = collect_lanes(repo, now=NOW)[0]
    assert any(
        item.source == "events/lifecycle.jsonl" and item.detail == "evidence unavailable" for item in lane.evidence
    )


def test_newest_runs_win_return_limit_and_truncation_fails_unknown(tmp_path, capsys):
    from brigade.runs_patrol import patrol

    repo = _repo(tmp_path)
    newest = _write_run(repo, "20260820-120000-aaaa0000", status="ok")
    for index in range(54):
        older = _write_run(repo, f"20260820-120000-zzzz{index:04d}", status="ok")
        os.utime(older, (NOW.timestamp() - 100, NOW.timestamp() - 100))
    os.utime(newest, (NOW.timestamp(), NOW.timestamp()))
    (newest.parent / "junk entry").mkdir()
    assert patrol(target=repo, now=NOW, limit=2, json_output=True, fail_on="unknown") == 1
    payload = json.loads(capsys.readouterr().out)
    assert payload["truncated"] is True
    assert len(payload["lanes"]) == 2
    assert newest.name in {row["run_id"] for row in payload["lanes"]}
    assert patrol(target=repo, now=NOW, limit=2) == 0
    assert "truncated" in capsys.readouterr().out


def test_claims_after_old_unclaimed_tasks_and_claim_truncation(tmp_path, capsys):
    from brigade.runs_patrol import patrol

    repo = _repo(tmp_path)
    ledger = _ledger(repo)
    claim = json.loads(ledger.read_text())["tasks"][0]
    tasks = [{"id": f"old-{index}", "status": "done"} for index in range(180)]
    tasks.append(claim)
    ledger.write_text(json.dumps({"tasks": tasks}))
    assert patrol(target=repo, now=NOW, limit=1, json_output=True, fail_on="claim-without-run") == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["lanes"][0]["claim_id"] == "claim-1"
    assert payload["lanes"][0]["state"] == "unknown"
    assert payload["truncated"] is False
    tasks.append({**claim, "id": "task-2", "claim": {**claim["claim"], "claim_id": "claim-2"}})
    ledger.write_text(json.dumps({"tasks": tasks}))
    assert patrol(target=repo, now=NOW, limit=1, json_output=True, fail_on="unknown") == 1
    assert json.loads(capsys.readouterr().out)["truncated"] is True


@pytest.mark.parametrize("old_claim", [None, "old-claim"])
def test_unmatched_claim_is_unknown_beside_realistic_active_run(tmp_path, monkeypatch, old_claim):
    from brigade.run_lane_state import Thresholds, classify, collect_lanes

    repo = _repo(tmp_path)
    _ledger(repo)
    if old_claim:
        _write_run(repo, "20260820-120000-oldrun01", status="ok", extra={"task_id": "task-1", "claim_id": old_claim})
    run_dir = _write_run(repo, "20260820-120000-patrol01")
    _write_stale_lock(repo, run_dir)
    monkeypatch.setattr(runguard, "_pid_is_active", lambda pid: True)
    signal = run_dir / "events/worker.jsonl"
    signal.parent.mkdir()
    signal.write_text("{}\n")
    os.utime(signal, (NOW.timestamp() - 10, NOW.timestamp() - 10))
    lanes = collect_lanes(repo, now=NOW)
    claim = next(lane for lane in lanes if lane.claim)
    result = classify(claim, NOW, Thresholds())
    assert result.state == "unknown"
    assert "no task-to-run join key" in result.reason
    active = next(lane for lane in lanes if lane.run_dir == run_dir)
    assert classify(active, NOW, Thresholds()).state == "working"


def test_reclaimed_task_does_not_join_old_terminal_run(tmp_path):
    from brigade.run_lane_state import Thresholds, classify, collect_lanes

    repo = _repo(tmp_path)
    _ledger(repo)
    _write_run(repo, "20260820-120000-patrol01", status="ok", extra={"task_id": "task-1", "claim_id": "old-claim"})
    lanes = collect_lanes(repo, now=NOW)
    assert len(lanes) == 2
    claim = next(lane for lane in lanes if lane.claim)
    assert classify(claim, NOW, Thresholds()).state == "unknown"


def test_worktree_inventory_has_separate_cap_and_only_unverified_lanes_fail(tmp_path, monkeypatch):
    from brigade import proc, run_lane_state

    repo = _repo(tmp_path)
    run_dir = _write_run(repo, "20260820-120000-patrol01")
    _write_stale_lock(repo, run_dir)
    monkeypatch.setattr(runguard, "_pid_is_active", lambda pid: True)
    (repo / "tracked.txt").write_text("progress")
    os.utime(repo / "tracked.txt", (NOW.timestamp() - 10, NOW.timestamp() - 10))
    real_git = run_lane_state._git
    paths = [repo, *(tmp_path / f"linked-{index}" for index in range(60))]

    def git(path, *args):
        if args[:2] == ("worktree", "list"):
            return proc.Result(0, "".join(f"worktree {item}\0" for item in paths), "")
        return real_git(path, *args)

    monkeypatch.setattr(run_lane_state, "_git", git)
    lanes = run_lane_state.collect_lanes(repo, now=NOW, limit=1)
    assert run_lane_state.classify(lanes[0], NOW, run_lane_state.Thresholds()).state == "working"
    monkeypatch.setattr(run_lane_state, "WORKTREE_SCAN_LIMIT", 1, raising=False)
    paths[1].mkdir()
    outside = _write_run(repo, "20260820-120000-patrol02", extra={"worktree": str(paths[1])})
    lanes = run_lane_state.collect_lanes(repo, now=NOW)
    states = {lane.run_dir: run_lane_state.classify(lane, NOW, run_lane_state.Thresholds()).state for lane in lanes}
    assert states == {run_dir: "working", outside: "unknown"}
    assert lanes.truncated


@pytest.mark.parametrize("terminal_source", ["snapshot", "journal"])
def test_terminal_runs_skip_secondary_evidence(tmp_path, monkeypatch, terminal_source):
    from brigade import run_journal, run_lane_state

    repo = _repo(tmp_path)
    run_dir = _write_run(
        repo,
        "20260820-120000-patrol01",
        status="ok" if terminal_source == "snapshot" else "dispatching",
        extra={"finished_at": OLD_STARTED, "worktree": str(repo / "pruned")},
    )
    (run_dir / "roster.json").write_text("corrupt")
    if terminal_source == "journal":
        run_journal.append_event(
            run_dir / "events/lifecycle.jsonl",
            run_id=run_dir.name,
            event_type="run.completed",
            payload={"status": "ok"},
            idempotency_key="complete",
            expected_previous_sequence=0,
            recorded_at="2026-08-20T12:00:00.000000Z",
        )
    real_git = run_lane_state._git

    def git(path, *args):
        assert args[0] != "status", "terminal run must not query git status"
        return real_git(path, *args)

    monkeypatch.setattr(run_lane_state, "_git", git)
    lane = run_lane_state.collect_lanes(repo, now=NOW)[0]
    assert run_lane_state.classify(lane, NOW, run_lane_state.Thresholds()).state == "complete"


def test_shared_worktree_status_is_collected_once(tmp_path, monkeypatch):
    from brigade import run_lane_state

    repo = _repo(tmp_path)
    for index in range(3):
        _write_run(repo, f"20260820-120000-patrol{index:02d}")
    real_git = run_lane_state._git
    calls = []

    def git(path, *args):
        if args[0] == "status":
            calls.append(path)
        return real_git(path, *args)

    monkeypatch.setattr(run_lane_state, "_git", git)
    run_lane_state.collect_lanes(repo, now=NOW)
    assert calls == [repo]


def test_shared_workspace_lock_is_collected_once(tmp_path, monkeypatch):
    from brigade.run_lane_state import Thresholds, classify, collect_lanes

    repo = _repo(tmp_path)
    _write_run(repo, "20260820-120000-patrol01", status="ok", extra={"finished_at": OLD_STARTED})
    _write_run(repo, "20260820-120000-patrol02", status="failed", extra={"finished_at": OLD_STARTED})
    active = _write_run(repo, "20260820-120000-patrol03")
    _write_stale_lock(repo, active)
    signal = active / "events/worker.jsonl"
    signal.parent.mkdir()
    signal.write_text("{}\n")
    os.utime(signal, (NOW.timestamp(), NOW.timestamp()))
    real_lock_path = runguard.lock_path
    path_calls = []
    pid_calls = []

    def lock_path(workspace):
        path_calls.append(workspace)
        return real_lock_path(workspace)

    def live(pid):
        pid_calls.append(pid)
        return True

    monkeypatch.setattr(runguard, "lock_path", lock_path)
    monkeypatch.setattr(runguard, "_pid_is_active", live)
    lanes = collect_lanes(repo, now=NOW)
    states = {lane.lane_id: classify(lane, NOW, Thresholds()).state for lane in lanes}
    assert states == {
        "20260820-120000-patrol01": "complete",
        "20260820-120000-patrol02": "failed",
        "20260820-120000-patrol03": "working",
    }
    assert path_calls == [repo]
    assert len(pid_calls) == 1


def test_changed_external_symlink_is_skipped(tmp_path, monkeypatch):
    from brigade.run_lane_state import Thresholds, classify, collect_lanes

    repo = _repo(tmp_path)
    run_dir = _write_run(repo, "20260820-120000-patrol01")
    _write_stale_lock(repo, run_dir)
    monkeypatch.setattr(runguard, "_pid_is_active", lambda pid: True)
    (repo / "external").symlink_to(tmp_path / "outside")
    (repo / "tracked.txt").write_text("progress")
    os.utime(repo / "tracked.txt", (NOW.timestamp() - 10, NOW.timestamp() - 10))
    lane = collect_lanes(repo, now=NOW)[0]
    assert classify(lane, NOW, Thresholds()).state == "working"


@pytest.mark.parametrize("live,expected", [(False, "dead"), (True, "working")])
def test_run_liveness_precedes_missing_worktree(tmp_path, monkeypatch, live, expected):
    from brigade.run_lane_state import Thresholds, classify, collect_lanes

    repo = _repo(tmp_path)
    run_dir = _write_run(repo, "20260820-120000-patrol01", extra={"worktree": str(repo / "pruned")})
    _write_stale_lock(repo, run_dir)
    monkeypatch.setattr(runguard, "_pid_is_active", lambda pid: live)
    signal = run_dir / "events/worker.jsonl"
    signal.parent.mkdir()
    signal.write_text("{}\n")
    os.utime(signal, (NOW.timestamp() - 10, NOW.timestamp() - 10))
    lane = collect_lanes(repo, now=NOW)[0]
    assert classify(lane, NOW, Thresholds()).state == expected


@pytest.mark.parametrize("claim_id", ["dispatch-intent-1", "newer-claim"])
def test_dispatch_intent_joins_only_exact_run_and_claim_generation(tmp_path, claim_id):
    from brigade.run_lane_state import collect_lanes

    repo = _repo(tmp_path)
    run_dir = _write_run(repo, "20260820-120000-patrol01", status="ok")
    intent_dir = repo / ".brigade/dispatch/dispatch-example/intents"
    intent_dir.mkdir(parents=True)
    (intent_dir / "intent-1.json").write_text(
        json.dumps(
            {"intent_id": "intent-1", "run_id": run_dir.name, "task_id": "task-1", "claim_id": "dispatch-intent-1"}
        )
    )
    ledger = repo / ".brigade/work/tasks.json"
    ledger.parent.mkdir()
    ledger.write_text(
        json.dumps(
            {
                "tasks": [
                    {
                        "id": "task-1",
                        "status": "in_progress",
                        "claim": {"claim_id": claim_id, "actor": "coder", "claimed_at": OLD_STARTED},
                    }
                ]
            }
        )
    )
    lanes = collect_lanes(repo, now=NOW)
    joined = next(lane for lane in lanes if lane.run_dir == run_dir)
    assert joined.intent["task_id"] == "task-1"
    if claim_id == "dispatch-intent-1":
        assert len(lanes) == 1
        assert joined.task["id"] == "task-1"
    else:
        assert joined.task is None
        assert len(lanes) == 2


@pytest.mark.parametrize("supplied", [False, True])
def test_intent_scan_budget_counts_unreadable_candidates(tmp_path, monkeypatch, supplied):
    from brigade import run_lane_state

    repo = _repo(tmp_path)
    _write_run(repo, "20260820-120000-patrol01", status="ok")
    root = repo / ".brigade/dispatch/dispatch-example/intents"
    root.mkdir(parents=True)
    for index in range(4):
        (root / f"intent-{index}.json").write_text("{bad JSON")
    monkeypatch.setattr(run_lane_state, "RUN_SCAN_LIMIT", 2)
    read = run_lane_state._read_json
    reads = []

    def counted(path):
        if path.parent == root:
            reads.append(path)
        return read(path)

    monkeypatch.setattr(run_lane_state, "_read_json", counted)
    lanes = run_lane_state.collect_lanes(repo, now=NOW, dispatch_intents=[{}] * 3 if supplied else None)
    assert len(reads) <= 2
    assert lanes.truncated
    assert not lanes[0].intent_scan_complete


def test_output_truncation_does_not_imply_incomplete_intent_scan(tmp_path):
    from brigade.run_lane_state import collect_lanes

    repo = _repo(tmp_path)
    _write_run(repo, "20260820-120000-patrol01", status="ok")
    _write_run(repo, "20260820-120001-patrol02", status="ok")
    lanes = collect_lanes(repo, now=NOW, limit=1)
    assert lanes.truncated
    assert lanes[0].intent_scan_complete
