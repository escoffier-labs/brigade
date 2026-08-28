"""Local orphan-run reaper, Hub stale-history aging, and work-brief surfacing."""

from __future__ import annotations

import json
import os
import shutil
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from brigade import cli
from brigade import doctor
from brigade import dogfood_cmd
from brigade import fleet_command_deck as deck
from brigade import fleet_dashboard
from brigade import fleet_hub
from brigade import localio
from brigade import proc
from brigade import run_checkpoint
from brigade import run_dirfd
from brigade import run_events
from brigade import run_journal
from brigade import run_lifecycle
from brigade import run_projector
from brigade import run_reap
from brigade import run_redaction
from brigade import runguard
from brigade import runs_cmd
from brigade import work_cmd
from tests.work_cmd_test_helpers import _init_git_repo


OLD_STARTED = "2026-08-20T12:00:00+00:00"
NOW = datetime(2026, 8, 28, 18, 0, 0, tzinfo=timezone.utc)
NODE = "11111111-1111-4111-8111-111111111111"
# A run that durably opted into lifecycle journaling. Legacy runs omit it and
# must stay snapshot-only through the reaper.
JOURNAL_REQUESTED = {"lifecycle_journal_requested": True}
AUTHORITY_REQUESTED = {"lifecycle_journal_requested": True, "run_journal_authority_requested": True}


def _git(repo: Path, *args: str) -> proc.Result:
    result = proc.run(["git", *args], cwd=repo)
    assert result.code == 0, result.stderr
    return result


def _repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init")
    _git(repo, "config", "user.email", "test@example.invalid")
    _git(repo, "config", "user.name", "Test User")
    (repo / "tracked.txt").write_text("base\n")
    _git(repo, "add", "tracked.txt")
    _git(repo, "commit", "-m", "initial")
    return repo


def _write_run(
    repo: Path,
    run_id: str,
    *,
    status: str = "dispatching",
    started_at: str = OLD_STARTED,
    extra: dict | None = None,
) -> Path:
    run_dir = repo / ".brigade" / "runs" / run_id
    run_dir.mkdir(parents=True)
    payload = {
        "task": "orphan task",
        "cwd": str(repo),
        "lock_workspace": str(repo),
        "orchestrator": "chef",
        "status": status,
        "started_at": started_at,
    }
    if extra:
        payload.update(extra)
    localio.write_json(run_dir / "run.json", payload)
    return run_dir


def _write_stale_lock(repo: Path, run_dir: Path, *, pid: int = 99999999) -> Path:
    lock_path = repo / ".brigade" / "run.lock"
    lock_path.mkdir(parents=True)
    (lock_path / "pid").write_text(f"{pid}\n")
    localio.write_json(
        lock_path / "owner.json",
        {
            "schema": "brigade.run_lock.v1",
            "owner_token": "owner",
            "pid": pid,
            "run_dir": str(run_dir.resolve()),
            "acquired_at": OLD_STARTED,
        },
    )
    return lock_path


def _reap(repo: Path, *, older_than: str = "2h", json_output: bool = True, now: datetime = NOW) -> int:
    return run_reap.reap(
        cwd=repo,
        runs_dir=None,
        older_than=older_than,
        json_output=json_output,
        now=now,
    )


def test_parse_older_than_default_and_units():
    assert run_reap.parse_older_than("2h") == timedelta(hours=2)
    assert run_reap.parse_older_than("30m") == timedelta(minutes=30)
    assert run_reap.parse_older_than("90s") == timedelta(seconds=90)
    assert run_reap.parse_older_than("1d") == timedelta(days=1)
    with pytest.raises(ValueError):
        run_reap.parse_older_than("nope")


def test_reap_terminalizes_stale_owner_as_orphaned(tmp_path, capsys):
    repo = _repo(tmp_path)
    (repo / "secret-name.txt").write_text("dirty\n")
    (repo / "tracked.txt").write_text("changed\n")
    run_dir = _write_run(repo, "20260820-120000-orphan01", extra=dict(JOURNAL_REQUESTED))
    _write_stale_lock(repo, run_dir)

    assert _reap(repo) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["schema"] == "brigade.runs-reap.v1"
    assert payload["reaped"][0]["run_id"] == "20260820-120000-orphan01"
    assert payload["reaped"][0]["uncommitted_change_count"] == 2
    dumped = json.dumps(payload)
    assert "secret-name.txt" not in dumped
    assert "tracked.txt" not in dumped

    meta = json.loads((run_dir / "run.json").read_text())
    assert meta["status"] == "orphaned"
    assert meta["last_observed_status"] == "dispatching"
    assert meta["uncommitted_change_count"] == 2
    assert meta["orphaned_at"]
    assert meta["finished_at"]
    assert "secret-name.txt" not in json.dumps(meta)

    journal = run_dir / "events" / "lifecycle.jsonl"
    assert journal.is_file()
    events = run_journal.read_journal(journal).events
    assert events[-1].event_type == "run.orphaned"
    assert events[-1].payload["status"] == "orphaned"
    assert events[-1].payload["last_observed_status"] == "dispatching"
    assert events[-1].payload["uncommitted_change_count"] == 2
    assert "secret-name.txt" not in journal.read_text()


def test_reap_is_idempotent(tmp_path, capsys):
    repo = _repo(tmp_path)
    run_dir = _write_run(repo, "20260820-120000-orphan02", extra=dict(JOURNAL_REQUESTED))
    _write_stale_lock(repo, run_dir)
    assert _reap(repo) == 0
    first = json.loads((run_dir / "run.json").read_text())
    first_events = run_journal.read_journal(run_dir / "events" / "lifecycle.jsonl").events
    capsys.readouterr()

    assert _reap(repo) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["reaped"] == []
    assert payload["skipped"][0]["reason"] == "already-orphaned"
    second = json.loads((run_dir / "run.json").read_text())
    assert second == first
    assert run_journal.read_journal(run_dir / "events" / "lifecycle.jsonl").events == first_events


def _skipped_reasons(capsys, repo: Path) -> dict[str, str]:
    assert _reap(repo) == 0
    payload = json.loads(capsys.readouterr().out)
    return {row["run_id"]: row["reason"] for row in payload["skipped"]}


def test_reap_skips_live_owner(tmp_path, capsys):
    repo = _repo(tmp_path)
    live = _write_run(repo, "20260820-120000-live0001")
    _write_stale_lock(repo, live, pid=os.getpid())
    (repo / ".brigade" / "run.lock" / "pid").write_text(f"{os.getpid()}\n")
    reasons = _skipped_reasons(capsys, repo)
    assert reasons["20260820-120000-live0001"] == "live"
    assert json.loads((live / "run.json").read_text())["status"] == "dispatching"


def test_reap_skips_too_young_run(tmp_path, capsys):
    repo = _repo(tmp_path)
    young = _write_run(repo, "20260828-170000-young001", started_at="2026-08-28T17:00:00+00:00")
    _write_stale_lock(repo, young)
    reasons = _skipped_reasons(capsys, repo)
    assert reasons["20260828-170000-young001"] == "too-young"
    assert json.loads((young / "run.json").read_text())["status"] == "dispatching"


def test_revalidate_metadata_exact_threshold_is_too_young():
    """The contract is strictly older than the threshold; equality stays too-young."""
    started = NOW - timedelta(hours=2)
    reason = run_reap._revalidate_metadata(
        {"status": "dispatching", "started_at": started.isoformat()},
        older_than=timedelta(hours=2),
        now=NOW,
    )
    assert reason == "too-young"


def test_reap_missing_cwd_error_is_bounded(tmp_path, capsys):
    nasty = tmp_path / "cwd\n\x1b[31mINJECT"
    nasty.write_text("not-a-directory\n")
    assert (
        run_reap.reap(
            cwd=nasty,
            runs_dir=None,
            older_than="2h",
            json_output=True,
            now=NOW,
        )
        == 2
    )
    err = capsys.readouterr().err
    assert err == "error: --cwd is not a directory\n"
    assert "INJECT" not in err
    assert "\x1b" not in err
    assert nasty.name not in err
    assert str(nasty.resolve()) not in err


def test_reap_missing_runs_dir_error_is_bounded(tmp_path, capsys):
    repo = _repo(tmp_path)
    nasty = tmp_path / "runs\n\x1b[31mINJECT"
    assert (
        run_reap.reap(
            cwd=repo,
            runs_dir=nasty,
            older_than="2h",
            json_output=True,
            now=NOW,
        )
        == 2
    )
    err = capsys.readouterr().err
    assert err == "error: runs directory not found\n"
    assert "INJECT" not in err
    assert "\x1b" not in err
    assert nasty.name not in err
    assert str(nasty.resolve()) not in err


def test_reap_skips_malformed_symlink_and_ambiguous(tmp_path, capsys):
    repo = _repo(tmp_path)
    runs = repo / ".brigade" / "runs"
    runs.mkdir(parents=True)
    malformed = runs / "20260820-120000-badjson1"
    malformed.mkdir()
    (malformed / "run.json").write_text("{not-json")
    outside = tmp_path / "outside-run"
    outside.mkdir()
    localio.write_json(outside / "run.json", {"status": "dispatching", "started_at": OLD_STARTED})
    (runs / "20260820-120000-symlink1").symlink_to(outside)
    ambiguous = _write_run(repo, "20260820-120000-nolock01")
    reasons = _skipped_reasons(capsys, repo)
    assert reasons["20260820-120000-badjson1"] == "malformed"
    assert reasons["20260820-120000-symlink1"] == "symlink"
    assert reasons["20260820-120000-nolock01"] == "ambiguous"
    assert json.loads((ambiguous / "run.json").read_text())["status"] == "dispatching"


def test_symlinked_default_runs_root_never_terminalizes_the_outside_target(tmp_path, capsys):
    repo = _repo(tmp_path)
    outside_root = tmp_path / "outside-runs"
    run_id = "20260820-120000-symroot1"
    outside_run = outside_root / run_id
    outside_run.mkdir(parents=True)
    localio.write_json(
        outside_run / "run.json",
        {
            "task": "orphan task",
            "cwd": str(repo),
            "lock_workspace": str(repo),
            "orchestrator": "chef",
            "status": "dispatching",
            "started_at": OLD_STARTED,
        },
    )
    brigade = repo / ".brigade"
    brigade.mkdir(exist_ok=True)
    (brigade / "runs").symlink_to(outside_root)
    _write_stale_lock(repo, outside_run)
    before = (outside_run / "run.json").read_bytes()

    assert _reap(repo) == 2
    err = capsys.readouterr().err
    assert err == "error: runs directory not found\n"
    assert (outside_run / "run.json").read_bytes() == before
    assert json.loads(before)["status"] == "dispatching"
    lock_owner = json.loads((repo / ".brigade" / "run.lock" / "owner.json").read_text())
    assert lock_owner["pid"] == 99999999


def test_reap_one_winner_under_race(tmp_path):
    repo = _repo(tmp_path)
    run_dir = _write_run(repo, "20260820-120000-race0001", extra=dict(JOURNAL_REQUESTED))
    _write_stale_lock(repo, run_dir)
    results: list[int] = []

    def _worker() -> None:
        results.append(
            run_reap.reap(
                cwd=repo,
                runs_dir=None,
                older_than="2h",
                json_output=True,
                now=NOW,
            )
        )

    threads = [threading.Thread(target=_worker) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=10)
    assert results == [0, 0]
    meta = json.loads((run_dir / "run.json").read_text())
    assert meta["status"] == "orphaned"
    events = run_journal.read_journal(run_dir / "events" / "lifecycle.jsonl").events
    assert sum(1 for event in events if event.event_type == "run.orphaned") == 1


def _racing_lock_after_claim(run_dir: Path, mutate):
    real_lock = runguard.run_lock

    def _racing_lock(*args, **kwargs):
        context = real_lock(*args, **kwargs)

        class _Wrapper:
            def __enter__(self):
                path = context.__enter__()
                payload = json.loads((run_dir / "run.json").read_text())
                mutate(payload)
                localio.write_json(run_dir / "run.json", payload)
                return path

            def __exit__(self, *exc):
                return context.__exit__(*exc)

        return _Wrapper()

    return _racing_lock


def test_reap_skips_concurrently_changed_run(tmp_path, capsys, monkeypatch):
    repo = _repo(tmp_path)
    run_dir = _write_run(repo, "20260820-120000-changed1")
    _write_stale_lock(repo, run_dir)

    def _to_terminal(payload: dict) -> None:
        payload["status"] = "ok"
        payload["finished_at"] = "2026-08-28T17:59:00+00:00"

    monkeypatch.setattr(runguard, "run_lock", _racing_lock_after_claim(run_dir, _to_terminal))
    assert _reap(repo) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["reaped"] == []
    assert payload["skipped"][0]["reason"] == "concurrently-changed"
    assert json.loads((run_dir / "run.json").read_text())["status"] == "ok"


def test_nonterminal_cas_loss_retains_the_claimed_lock(tmp_path, capsys, monkeypatch):
    repo = _repo(tmp_path)
    run_dir = _write_run(repo, "20260820-120000-casret01")
    _write_stale_lock(repo, run_dir)

    def _keep_nonterminal(payload: dict) -> None:
        payload["task"] = "concurrent edit"

    monkeypatch.setattr(runguard, "run_lock", _racing_lock_after_claim(run_dir, _keep_nonterminal))
    assert _reap(repo) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["reaped"] == []
    assert payload["skipped"][0]["reason"] == "concurrently-changed"
    current = json.loads((run_dir / "run.json").read_text())
    assert current["status"] == "dispatching"
    assert current["task"] == "concurrent edit"
    lock_path = repo / ".brigade" / "run.lock"
    assert lock_path.is_dir()
    owner = json.loads((lock_path / "owner.json").read_text())
    assert Path(owner["run_dir"]) == run_dir.resolve()
    assert owner["pid"] == os.getpid()
    assert runguard.run_lock_state(repo, run_dir) == "live"


def test_terminal_concurrent_update_releases_the_claimed_lock(tmp_path, capsys, monkeypatch):
    repo = _repo(tmp_path)
    run_dir = _write_run(repo, "20260820-120000-casrel01")
    _write_stale_lock(repo, run_dir)

    def _to_terminal(payload: dict) -> None:
        payload["status"] = "ok"
        payload["finished_at"] = "2026-08-28T17:59:00+00:00"

    monkeypatch.setattr(runguard, "run_lock", _racing_lock_after_claim(run_dir, _to_terminal))
    assert _reap(repo) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["reaped"] == []
    assert payload["skipped"][0]["reason"] == "concurrently-changed"
    assert json.loads((run_dir / "run.json").read_text())["status"] == "ok"
    assert not (repo / ".brigade" / "run.lock").exists()
    assert runguard.run_lock_state(repo, run_dir) == "absent"


def test_runs_reap_cli_default_older_than_and_readable(tmp_path, capsys):
    repo = _repo(tmp_path)
    run_dir = _write_run(repo, "20260820-120000-cli0001")
    _write_stale_lock(repo, run_dir)
    assert cli.main(["runs", "reap", "--cwd", str(repo)]) == 0
    out = capsys.readouterr().out
    assert "reaped: 1" in out
    assert "20260820-120000-cli0001" in out
    assert json.loads((run_dir / "run.json").read_text())["status"] == "orphaned"


def test_run_orphaned_is_terminal_across_contracts():
    assert "run.orphaned" in run_events.EVENT_TYPES
    assert run_events.EVENT_TYPES["run.orphaned"] == frozenset(
        {"status", "last_observed_status", "uncommitted_change_count"}
    )
    assert run_lifecycle.STATUS_EVENT_TYPE["orphaned"] == "run.orphaned"
    assert "run.orphaned" in fleet_hub.TERMINAL_STATES
    assert "run.orphaned" in deck.TERMINAL_STATES
    assert deck.is_terminal_state("run.orphaned")
    assert "orphaned_at" in run_projector.PRESERVED_FIELDS
    assert "last_observed_status" in run_projector.PRESERVED_FIELDS
    assert "uncommitted_change_count" in run_projector.PRESERVED_FIELDS
    assert "orphaned" in run_redaction.TERMINAL_STATUSES


def test_projector_derives_orphaned_and_keeps_privacy():
    created = run_events.build_event(
        run_id="20260820-120000-proj0001",
        sequence=1,
        event_type="run.created",
        payload={"status": "started"},
        idempotency_key="create-1",
        recorded_at="2026-08-20T12:00:00.000000Z",
        previous_digest=None,
    )
    orphaned = run_events.build_event(
        run_id="20260820-120000-proj0001",
        sequence=2,
        event_type="run.orphaned",
        payload={
            "status": "orphaned",
            "last_observed_status": "dispatching",
            "uncommitted_change_count": 2,
        },
        idempotency_key="orphan-1",
        recorded_at="2026-08-28T18:00:00.000000Z",
        previous_digest=created["event_digest"],
    )
    with pytest.raises(run_events.CanonicalizationError):
        run_events.build_event(
            run_id="20260820-120000-proj0001",
            sequence=2,
            event_type="run.orphaned",
            payload={"status": "orphaned", "filenames": ["secret-name.txt"]},
            idempotency_key="orphan-bad",
            recorded_at="2026-08-28T18:00:00.000000Z",
            previous_digest=created["event_digest"],
        )
    projection = run_projector.project_run_snapshot(
        {"status": "started", "schema": "brigade.run.v1"},
        [created, orphaned],
        journal_present=True,
    )
    assert projection.status == "orphaned"


def test_hub_history_ages_old_nonterminal_as_synthetic_stale(tmp_path):
    db = tmp_path / "fleet.db"
    conn = fleet_hub.init_db(db)
    now = datetime(2026, 8, 28, 18, 0, 0, tzinfo=timezone.utc)
    conn.execute(
        "INSERT INTO events (node_id, run_id, sequence, digest, repo, seat, harness, state, ts, exit_status, capability_fingerprint, received_at) "
        "VALUES (?, ?, 1, 'old', 'repo', 'coder', 'cursor', 'run.dispatch.requested', ?, NULL, NULL, ?)",
        (NODE, "old-run", now.isoformat(), (now - timedelta(hours=30)).isoformat()),
    )
    conn.execute(
        "INSERT INTO events (node_id, run_id, sequence, digest, repo, seat, harness, state, ts, exit_status, capability_fingerprint, received_at) "
        "VALUES (?, ?, 1, 'fresh', 'repo', 'coder', 'cursor', 'run.dispatch.requested', ?, NULL, NULL, ?)",
        (NODE, "fresh-run", now.isoformat(), (now - timedelta(minutes=5)).isoformat()),
    )
    conn.commit()
    active = fleet_hub.latest_status(conn, now=now)
    history = fleet_hub.latest_status(conn, include_all=True, now=now)
    conn.close()
    assert {row["run_id"] for row in active} == {"fresh-run"}
    by_id = {row["run_id"]: row for row in history}
    assert by_id["old-run"]["state"] == "run.stale"
    assert by_id["old-run"]["original_state"] == "run.dispatch.requested"
    assert by_id["fresh-run"]["state"] == "run.dispatch.requested"
    assert "original_state" not in by_id["fresh-run"]


def test_stale_history_threshold_is_bounded_and_separate_from_liveness(tmp_path):
    assert fleet_hub.ACTIVE_EVENT_TTL_SECONDS == 30 * 60
    assert deck.DeckConfig().stale_after_seconds == 1800
    assert deck.DeckConfig().stale_history_after_seconds == 86400
    path = tmp_path / "deck.json"
    path.write_text(
        json.dumps(
            {
                "stations": [{"node_id": NODE, "name": "Alpha", "capacity": 2}],
                "stale_history_after_seconds": 3600,
            }
        )
    )
    assert deck.load_config(path).stale_history_after_seconds == 3600
    path.write_text(
        json.dumps(
            {
                "stations": [{"node_id": NODE, "name": "Alpha", "capacity": 2}],
                "stale_history_after_seconds": 60,
            }
        )
    )
    with pytest.raises(deck.DeckConfigError):
        deck.load_config(path)
    assert fleet_dashboard.bucket_for("run.stale", age_seconds=0) == "stale"
    assert fleet_dashboard.bucket_for("run.stale", age_seconds=None) == "stale"
    assert fleet_dashboard.bucket_for("run.orphaned", age_seconds=0) == "interrupted"


def test_work_brief_reports_orphaned_runs_and_dirty_trap(tmp_path, monkeypatch, capsys):
    _init_git_repo(tmp_path)
    dogfood_cmd.init(target=tmp_path)
    run_dir = tmp_path / ".brigade" / "runs" / "20260820-120000-brief001"
    run_dir.mkdir(parents=True)
    localio.write_json(
        run_dir / "run.json",
        {
            "status": "orphaned",
            "started_at": OLD_STARTED,
            "orphaned_at": "2026-08-28T18:00:00+00:00",
            "last_observed_status": "dispatching",
            "uncommitted_change_count": 3,
            "task": "stuck work",
        },
    )
    monkeypatch.setattr(work_cmd.helpers.shutil, "which", lambda name: f"/usr/bin/{name}")
    assert work_cmd.brief(target=tmp_path) == 0
    out = capsys.readouterr().out
    assert "orphaned_runs: 1" in out
    assert "20260820-120000-brief001" in out
    assert "dirty=3" in out
    assert "orphaned_trap:" in out
    assert "brigade runs show" in out
    capsys.readouterr()
    assert work_cmd.brief(target=tmp_path, json_output=True) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["orphaned_runs"][0]["uncommitted_change_count"] == 3
    assert "secret-name.txt" not in json.dumps(payload)
    assert payload["orphaned_runs"][0]["suggested_command"].startswith("brigade runs show")


def _journal_events(run_dir: Path):
    return run_journal.read_journal(run_dir / "events" / "lifecycle.jsonl").events


def _doctor_recovery(repo: Path) -> tuple[str, str, str]:
    return doctor._check_recovery_checkpoints(repo, full=True)


def _bootstrap_journaled_run(repo: Path, run_id: str, *, authority: bool) -> Path:
    """Create a run the way a real dispatch does, through the sanctioned writer.

    The run.json durable request fields are written first, then one status
    write under the run lock activates the journal, publishes the first
    recovery checkpoint, and (for an authority run) projects run.json.
    """
    from brigade import aboyeur, receipt_schema

    extra = dict(AUTHORITY_REQUESTED if authority else JOURNAL_REQUESTED)
    run_dir = _write_run(repo, run_id, status="planning", extra=extra)
    payload = json.loads((run_dir / "run.json").read_text())
    payload["status"] = "dispatching"
    with runguard.run_lock(repo, run_dir=run_dir):
        aboyeur._write_json(run_dir / "run.json", receipt_schema.stamp_run_receipt(payload))
    _write_stale_lock(repo, run_dir)
    return run_dir


def test_reap_leaves_a_legacy_snapshot_only_run_without_a_journal(tmp_path, capsys):
    """A run that never requested journaling is terminalized snapshot-only."""
    repo = _repo(tmp_path)
    run_dir = _write_run(repo, "20260820-120000-legacy01")
    _write_stale_lock(repo, run_dir)

    assert _reap(repo) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["reaped"][0]["run_id"] == "20260820-120000-legacy01"

    meta = json.loads((run_dir / "run.json").read_text())
    assert meta["status"] == "orphaned"
    assert meta["last_observed_status"] == "dispatching"
    # No journal is manufactured, and no projection metadata is invented.
    assert not (run_dir / "events" / "lifecycle.jsonl").exists()
    assert not (run_dir / "events" / "recovery-checkpoints").exists()
    for derived in ("projector_version", "journal_present", "journal_last_sequence", "journal_last_event_digest"):
        assert derived not in meta

    status, _name, detail = _doctor_recovery(repo)
    assert status == doctor.OK, detail
    assert "fail=0" in detail
    assert "omitted=1" in detail


def test_reap_of_a_journal_requested_run_writes_a_checkpoint_and_orphaned_pair(tmp_path, capsys):
    repo = _repo(tmp_path)
    run_dir = _bootstrap_journaled_run(repo, "20260820-120000-journal1", authority=False)
    before = len(_journal_events(run_dir))

    assert _reap(repo) == 0
    capsys.readouterr()

    events = _journal_events(run_dir)
    appended = events[before:]
    assert [event.event_type for event in appended] == ["run.snapshot.checkpointed", "run.orphaned"]
    assert appended[-1].payload["status"] == "orphaned"
    assert appended[-1].payload["last_observed_status"] == "dispatching"
    checkpoint = run_checkpoint.checkpoint_path(run_dir, appended[0].payload["sha256"])
    assert checkpoint.is_file()
    assert json.loads((run_dir / "run.json").read_text())["status"] == "orphaned"

    status, _name, detail = _doctor_recovery(repo)
    assert status == doctor.OK, detail
    assert "fail=0" in detail


def test_reap_of_an_authoritative_run_keeps_projected_cursor_parity(tmp_path, capsys):
    repo = _repo(tmp_path)
    run_dir = _bootstrap_journaled_run(repo, "20260820-120000-authori1", authority=True)
    bootstrapped = json.loads((run_dir / "run.json").read_text())
    assert bootstrapped["journal_present"] is True
    before = len(_journal_events(run_dir))

    assert _reap(repo) == 0
    capsys.readouterr()

    events = _journal_events(run_dir)
    appended = events[before:]
    assert [event.event_type for event in appended] == ["run.snapshot.checkpointed", "run.orphaned"]

    meta = json.loads((run_dir / "run.json").read_text())
    assert meta["status"] == "orphaned"
    assert meta["last_observed_status"] == "dispatching"
    # Projected cursor parity: run.json's saved cursor is the journal tail.
    assert meta["projector_version"] == run_projector.PROJECTOR_VERSION
    assert meta["journal_present"] is True
    assert meta["journal_last_sequence"] == len(events)
    assert meta["journal_last_event_digest"] == events[-1].event_digest

    # No lifecycle projection drift: run.json is exactly the projection of the
    # latest stripped checkpoint base over the verified event sequence.
    latest = run_checkpoint.latest_checkpoint_event(events)
    base = json.loads(run_checkpoint.checkpoint_path(run_dir, latest.payload["sha256"]).read_bytes())
    projection = run_projector.project_run_snapshot(base, events, journal_present=True)
    assert projection.to_bytes() == (run_dir / "run.json").read_bytes()
    assert projection.status == "orphaned"

    status, _name, detail = _doctor_recovery(repo)
    assert status == doctor.OK, detail
    assert "fail=0" in detail


def test_run_lock_claim_refuses_a_stale_lock_owned_by_another_run(tmp_path):
    """stale_action='claim' fails closed on a run_dir mismatch, deleting nothing."""
    repo = _repo(tmp_path)
    mine = _write_run(repo, "20260820-120000-mine0001")
    theirs = _write_run(repo, "20260820-120000-theirs01")
    _write_stale_lock(repo, theirs)
    lock = repo / ".brigade" / "run.lock"
    owner_before = (lock / "owner.json").read_text()

    with pytest.raises(runguard.RunLockError) as excinfo:
        with runguard.run_lock(repo, run_dir=mine, wait_seconds=0, stale_action="claim"):
            pass
    assert "does not belong to this run" in str(excinfo.value)
    assert lock.is_dir()
    assert (lock / "owner.json").read_text() == owner_before
    assert json.loads((theirs / "run.json").read_text())["status"] == "dispatching"


def test_run_lock_claim_requires_a_run_dir():
    with pytest.raises(ValueError, match="requires run_dir"):
        with runguard.run_lock(Path("."), stale_action="claim"):
            pass


def test_reap_fails_closed_when_the_lock_owner_changes_after_preflight(tmp_path, capsys, monkeypatch):
    """TOCTOU: preflight saw a matching stale lock; the claim path sees another run."""
    repo = _repo(tmp_path)
    mine = _write_run(repo, "20260820-120000-toctou01")
    theirs = _write_run(repo, "20260820-120000-toctou02", status="ok", started_at=OLD_STARTED)
    _write_stale_lock(repo, theirs)
    lock = repo / ".brigade" / "run.lock"
    owner_before = (lock / "owner.json").read_text()

    monkeypatch.setattr(runguard, "run_lock_state", lambda workspace, run_dir: "stale")
    assert _reap(repo) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["reaped"] == []
    reasons = {row["run_id"]: row["reason"] for row in payload["skipped"]}
    assert reasons["20260820-120000-toctou01"] == "concurrently-changed"
    # The foreign claim survives untouched for its own owner to recover.
    assert lock.is_dir()
    assert (lock / "owner.json").read_text() == owner_before
    assert json.loads((mine / "run.json").read_text())["status"] == "dispatching"


def test_list_orphaned_runs_scan_is_bounded(tmp_path, monkeypatch):
    repo = _repo(tmp_path)
    assert run_reap.ORPHAN_SCAN_LIMIT == 50
    for index in range(run_reap.ORPHAN_SCAN_LIMIT + 10):
        _write_run(repo, f"20260820-1200{index:02d}-bound{index:03d}")
    buried = _write_run(repo, "20260819-120000-buried01")
    localio.write_json(
        buried / "run.json",
        {"status": "orphaned", "last_observed_status": "dispatching", "uncommitted_change_count": 1},
    )

    reads: list[Path] = []
    real_read = run_reap._read_run_bytes

    def _counting_read(run_dir: Path):
        reads.append(run_dir)
        return real_read(run_dir)

    monkeypatch.setattr(run_reap, "_read_run_bytes", _counting_read)
    assert run_reap.list_orphaned_runs(repo) == []
    assert len(reads) == run_reap.ORPHAN_SCAN_LIMIT
    # The bound is a window, not a filter: a wider scan still finds the orphan.
    reads.clear()
    rows = run_reap.list_orphaned_runs(repo, scan_limit=200)
    assert [row["run_id"] for row in rows] == ["20260819-120000-buried01"]


def test_command_deck_buckets_synthetic_stale_history():
    assert deck.bucket_for("run.stale", age_seconds=0, stale_after_seconds=1800) == "stale"
    assert deck.bucket_for("run.stale", age_seconds=None, stale_after_seconds=1800) == "stale"
    assert deck.bucket_for("run.stale", age_seconds=999_999, stale_after_seconds=1800) == "stale"
    assert deck.bucket_for("run.stale", age_seconds=0, stale_after_seconds=1800) == fleet_dashboard.bucket_for(
        "run.stale", age_seconds=0
    )


def _pending_stale_claim(repo: Path, run_dir: Path, *, token: str = "pending-other") -> Path:
    lock = repo / ".brigade" / "run.lock"
    stale = lock.with_name(f".{lock.name}.unrelated.stale")
    stale.mkdir(parents=True)
    (stale / "pid").write_text("99999999\n")
    localio.write_json(
        stale / "owner.json",
        {
            "schema": "brigade.run_lock.v1",
            "owner_token": token,
            "pid": 99999999,
            "run_dir": str(run_dir.resolve()),
            "acquired_at": OLD_STARTED,
        },
    )
    return stale


def test_run_lock_claim_leaves_unrelated_pending_claim_untouched(tmp_path):
    """stale_action='claim' must not recover or terminalize a foreign pending claim."""
    repo = _repo(tmp_path)
    mine = _write_run(repo, "20260820-120000-claim001")
    other = _write_run(repo, "20260820-120000-claim002")
    _write_stale_lock(repo, mine)
    pending = _pending_stale_claim(repo, other)
    pending_owner = (pending / "owner.json").read_text()
    other_before = (other / "run.json").read_bytes()

    with runguard.run_lock(repo, run_dir=mine, wait_seconds=0, stale_action="claim"):
        pass

    assert pending.is_dir()
    assert (pending / "owner.json").read_text() == pending_owner
    assert (other / "run.json").read_bytes() == other_before
    assert json.loads((other / "run.json").read_text())["status"] == "dispatching"


def test_reap_refuses_swapped_run_dir_and_does_not_touch_outside_run_json(tmp_path, capsys, monkeypatch):
    """A post-containment directory swap must not read or write an outside run.json."""
    repo = _repo(tmp_path)
    run_dir = _write_run(repo, "20260820-120000-swap0001", extra=dict(JOURNAL_REQUESTED))
    _write_stale_lock(repo, run_dir)
    outside = tmp_path / "outside-run"
    outside.mkdir()
    outside_json = outside / "run.json"
    localio.write_json(
        outside_json,
        {"status": "dispatching", "started_at": OLD_STARTED, "marker": "outside-canary"},
    )
    outside_before = outside_json.read_bytes()
    observed: list[str] = []
    real_read = run_reap._read_run_bytes
    real_write = __import__("brigade.aboyeur", fromlist=["_write_json"])._write_json

    def _guarded_read(path: Path):
        target = path / "run.json"
        try:
            if target.resolve() == outside_json.resolve():
                observed.append("read-outside")
        except OSError:
            pass
        return real_read(path)

    def _guarded_write(path: Path, payload: object) -> None:
        try:
            if path.resolve() == outside_json.resolve():
                observed.append("write-outside")
        except OSError:
            pass
        return real_write(path, payload)

    real_lock = runguard.run_lock

    def _swap_then_lock(*args, **kwargs):
        shutil.rmtree(run_dir)
        run_dir.symlink_to(outside, target_is_directory=True)
        return real_lock(*args, **kwargs)

    monkeypatch.setattr(run_reap, "_read_run_bytes", _guarded_read)
    monkeypatch.setattr("brigade.aboyeur._write_json", _guarded_write)
    monkeypatch.setattr(runguard, "run_lock", _swap_then_lock)

    assert _reap(repo) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["reaped"] == []
    assert payload["skipped"]
    assert all(row["reason"] in {"concurrently-changed", "malformed"} for row in payload["skipped"])
    assert outside_json.read_bytes() == outside_before
    assert "read-outside" not in observed
    assert "write-outside" not in observed
    assert json.loads(outside_before)["marker"] == "outside-canary"


def test_reap_json_and_cli_never_echo_control_bearing_run_id_or_status(tmp_path, capsys):
    repo = _repo(tmp_path)
    runs = repo / ".brigade" / "runs"
    runs.mkdir(parents=True)
    injected = "evil\n\x1b[31mINJECT"
    nasty = runs / injected
    nasty.mkdir()
    localio.write_json(
        nasty / "run.json",
        {
            "task": "inject",
            "cwd": str(repo),
            "lock_workspace": str(repo),
            "orchestrator": "chef",
            "status": "dispatching\n\x1b[31mINJECT",
            "started_at": OLD_STARTED,
        },
    )
    planted = _write_run(repo, "20260820-120000-safe0001")
    localio.write_json(
        planted / "run.json",
        {
            "status": "orphaned",
            "started_at": OLD_STARTED,
            "orphaned_at": "2026-08-28T18:00:00+00:00",
            "last_observed_status": "dispatching\n\x1b[31mINJECT",
            "uncommitted_change_count": 1,
        },
    )

    rows = run_reap.list_orphaned_runs(repo)
    listed = json.dumps(rows)
    assert injected not in listed
    assert "INJECT" not in listed
    assert "\x1b" not in listed
    assert rows
    assert all(row["run_id"] == "20260820-120000-safe0001" for row in rows)
    assert rows[0]["last_observed_status"] == "unknown"

    assert _reap(repo, json_output=True) == 0
    json_out = capsys.readouterr().out
    assert injected not in json_out
    assert "INJECT" not in json_out
    assert "\x1b" not in json_out
    payload = json.loads(json_out)
    for row in payload["skipped"]:
        assert row["run_id"] == "malformed" or row["run_id"] == "20260820-120000-safe0001"
        assert "\n" not in row["run_id"]
        assert "\x1b" not in row["run_id"]

    assert (
        run_reap.reap(
            cwd=repo,
            runs_dir=None,
            older_than="2h",
            json_output=False,
            now=NOW,
        )
        == 0
    )
    text = capsys.readouterr().out
    assert injected not in text
    assert "INJECT" not in text
    assert "\x1b" not in text


def test_write_refused_retains_the_claimed_lock_so_runs_recover_still_finds_the_run(tmp_path, capsys, monkeypatch):
    """A refused sanctioned write must leave a matching lock for `runs recover`.

    stale_action='claim' deletes the dead owner's lock as it takes over. If the
    reaper then released its own lock on a LifecycleJournalError/CheckpointError,
    the run would be left nonterminal with no lock and no matching .stale claim,
    and `brigade runs recover` would have nothing to match on.
    """
    repo = _repo(tmp_path)
    run_dir = _write_run(repo, "20260820-120000-refuse01")
    other = _write_run(repo, "20260820-120000-refuse02")
    _write_stale_lock(repo, run_dir)
    before = (run_dir / "run.json").read_bytes()

    def _refuse(*args, **kwargs):
        raise run_checkpoint.CheckpointError("checkpoint refused in test", category="test")

    monkeypatch.setattr(run_checkpoint, "write_checkpoint", _refuse)

    assert _reap(repo) == 0
    payload = json.loads(capsys.readouterr().out)
    reasons = {row["run_id"]: row["reason"] for row in payload["skipped"]}
    assert payload["reaped"] == []
    assert reasons["20260820-120000-refuse01"] == "write-refused"
    # The refused run is untouched and still nonterminal.
    assert (run_dir / "run.json").read_bytes() == before
    assert json.loads((run_dir / "run.json").read_text())["status"] == "dispatching"
    # The retained lock makes the rest of the scan skip: the second run now
    # sees a live lock in preflight instead of a claimable dead owner.
    assert reasons["20260820-120000-refuse02"] == "live"
    assert json.loads((other / "run.json").read_text())["status"] == "dispatching"

    # The lock was retained, not released, and still records this run.
    lock_path = repo / ".brigade" / "run.lock"
    assert lock_path.is_dir()
    owner = json.loads((lock_path / "owner.json").read_text())
    assert Path(owner["run_dir"]) == run_dir.resolve()
    assert owner["pid"] == os.getpid()
    assert runguard.run_lock_state(repo, run_dir) == "live"

    # Once the reaper process is gone the retained lock reads back as a
    # matching dead-owner stale lock, which is what recovery matches on.
    dead = 99999998
    (lock_path / "pid").write_text(f"{dead}\n")
    owner["pid"] = dead
    localio.write_json(lock_path / "owner.json", owner)
    assert runguard.run_lock_state(repo, run_dir) == "stale"
    assert runs_cmd.recover(run_dir, cwd=repo) == 0
    capsys.readouterr()
    assert not lock_path.exists()


def test_reap_fails_closed_when_the_platform_cannot_bind_a_run_directory(tmp_path, capsys, monkeypatch):
    """No no-follow directory primitive means no mutation at all, not a best effort."""
    repo = _repo(tmp_path)
    run_dir = _write_run(repo, "20260820-120000-nobind1", extra=dict(JOURNAL_REQUESTED))
    _write_stale_lock(repo, run_dir)
    before = (run_dir / "run.json").read_bytes()

    monkeypatch.setattr(run_reap, "_dirfd_identity_available", lambda: False)

    assert _reap(repo) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["reaped"] == []
    assert [row["reason"] for row in payload["skipped"]] == ["unbindable"]
    assert payload["skipped"][0]["run_id"] == "20260820-120000-nobind1"
    # Fail closed means nothing was written: no run.json edit, no journal, no
    # checkpoint, no shadow artifact, and the dead owner's lock is untouched.
    assert (run_dir / "run.json").read_bytes() == before
    assert not (run_dir / "events").exists()
    assert not (run_dir / "checkpoints").exists()
    assert not (run_dir / "shadow").exists()
    lock_owner = json.loads((repo / ".brigade" / "run.lock" / "owner.json").read_text())
    assert lock_owner["pid"] == 99999999


def test_identity_without_binding_is_unbindable_before_lock(tmp_path, capsys, monkeypatch):
    """Identity flags without dirfd binding support must not claim the lock."""
    from brigade import dirfd

    repo = _repo(tmp_path)
    run_dir = _write_run(repo, "20260820-120000-diverg01")
    _write_stale_lock(repo, run_dir)
    before = (run_dir / "run.json").read_bytes()
    lock_before = (repo / ".brigade" / "run.lock" / "owner.json").read_bytes()

    monkeypatch.setattr(dirfd, "available", lambda: False)

    assert _reap(repo) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["reaped"] == []
    assert [row["reason"] for row in payload["skipped"]] == ["unbindable"]
    assert payload["skipped"][0]["run_id"] == "20260820-120000-diverg01"
    assert (run_dir / "run.json").read_bytes() == before
    assert (repo / ".brigade" / "run.lock" / "owner.json").read_bytes() == lock_before
    lock_owner = json.loads((repo / ".brigade" / "run.lock" / "owner.json").read_text())
    assert lock_owner["pid"] == 99999999


def test_reap_swap_from_inside_the_sanctioned_writer_is_bounded_to_the_run_directory(tmp_path, capsys, monkeypatch):
    """A swap racing in from inside the writer path must not escape the runs root.

    The swap fires from within the sanctioned writer (the first checkpoint
    write), which is past every containment check the reaper can make. This
    pins the current, honest boundary: the reaper must not reap the run and
    must not leave the outside canary altered by the run.json replace.
    """
    repo = _repo(tmp_path)
    run_dir = _write_run(repo, "20260820-120000-inswap01", extra=dict(JOURNAL_REQUESTED))
    _write_stale_lock(repo, run_dir)
    outside = tmp_path / "outside-writer"
    outside.mkdir()
    outside_json = outside / "run.json"
    localio.write_json(
        outside_json,
        {"status": "dispatching", "started_at": OLD_STARTED, "marker": "outside-canary"},
    )
    outside_before = outside_json.read_bytes()

    def _swap_then_refuse(*args, **kwargs):
        shutil.rmtree(run_dir, ignore_errors=True)
        run_dir.symlink_to(outside, target_is_directory=True)
        raise run_checkpoint.CheckpointError("checkpoint refused after swap", category="test")

    monkeypatch.setattr(run_checkpoint, "write_checkpoint", _swap_then_refuse)

    assert _reap(repo) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["reaped"] == []
    assert [row["reason"] for row in payload["skipped"]] == ["write-refused"]
    assert outside_json.read_bytes() == outside_before
    assert json.loads(outside_before)["marker"] == "outside-canary"
    assert not (outside / "events").exists()
    assert not (outside / "checkpoints").exists()


# --- Descriptor-bound orphan-reaper transaction (phase 2) ---------------------
#
# The reaper binds the candidate run directory and runs the whole sanctioned
# transaction under that binding. Every swap below lands AFTER the binding is
# taken, from inside one stage of the writer. The invariant under test is the
# same each time: writes may only reach the original held run inode, or the
# stage fails closed. Nothing outside the run directory is ever written.


def _outside_canary(tmp_path: Path, name: str) -> tuple[Path, Path, bytes]:
    outside = tmp_path / name
    outside.mkdir()
    outside_json = outside / "run.json"
    localio.write_json(
        outside_json,
        {"status": "dispatching", "started_at": OLD_STARTED, "marker": "outside-canary"},
    )
    return outside, outside_json, outside_json.read_bytes()


def _swap_aside(run_dir: Path, outside: Path) -> Path:
    """Move the real run directory aside and point its name at ``outside``.

    Unlike an rmtree-then-symlink swap this keeps the original inode reachable
    under a new name, so a test can prove where a descriptor-relative write
    actually landed instead of only proving it failed.
    """
    moved = run_dir.with_name(f"{run_dir.name}.moved")
    run_dir.rename(moved)
    run_dir.symlink_to(outside, target_is_directory=True)
    return moved


def _swap_once(monkeypatch, module, attribute: str, run_dir: Path, outside: Path) -> list[Path]:
    """Patch one writer stage to swap the run directory on its first call."""
    real = getattr(module, attribute)
    moved: list[Path] = []

    def wrapper(*args, **kwargs):
        if not moved:
            moved.append(_swap_aside(run_dir, outside))
        return real(*args, **kwargs)

    monkeypatch.setattr(module, attribute, wrapper)
    return moved


def _assert_outside_untouched(outside: Path, outside_json: Path, before: bytes) -> None:
    assert outside_json.read_bytes() == before
    assert json.loads(before)["marker"] == "outside-canary"
    assert not (outside / "events").exists()
    assert not (outside / "revisions").exists()
    assert sorted(child.name for child in outside.iterdir()) == ["run.json"]


def _reap_under_swap(tmp_path, capsys, monkeypatch, module, attribute, *, run_id, extra=None):
    repo = _repo(tmp_path)
    run_dir = _write_run(repo, run_id, extra=dict(extra or JOURNAL_REQUESTED))
    _write_stale_lock(repo, run_dir)
    outside, outside_json, before = _outside_canary(tmp_path, f"outside-{run_id}")
    moved = _swap_once(monkeypatch, module, attribute, run_dir, outside)

    assert _reap(repo) == 0
    payload = json.loads(capsys.readouterr().out)
    assert moved, "the patched stage never ran, so no swap was exercised"
    _assert_outside_untouched(outside, outside_json, before)
    return payload, moved[0]


def test_swap_before_journal_activation_never_writes_outside_the_bound_run(tmp_path, capsys, monkeypatch):
    payload, moved = _reap_under_swap(
        tmp_path,
        capsys,
        monkeypatch,
        run_lifecycle,
        "prepare_lifecycle_journal",
        run_id="20260820-120000-bnd00001",
    )
    assert not (moved / "events" / "lifecycle.jsonl").exists() or (moved / "events" / "lifecycle.jsonl").is_file()
    assert payload["reaped"] or payload["skipped"]


def test_swap_before_the_checkpoint_write_never_writes_outside_the_bound_run(tmp_path, capsys, monkeypatch):
    payload, moved = _reap_under_swap(
        tmp_path,
        capsys,
        monkeypatch,
        run_checkpoint,
        "write_checkpoint",
        run_id="20260820-120000-bnd00002",
    )
    checkpoints = moved / "events" / run_checkpoint.CHECKPOINT_DIR_NAME
    if payload["reaped"]:
        assert checkpoints.is_dir()
    assert payload["reaped"] or payload["skipped"]


def test_swap_before_the_lifecycle_append_never_writes_outside_the_bound_run(tmp_path, capsys, monkeypatch):
    payload, moved = _reap_under_swap(
        tmp_path,
        capsys,
        monkeypatch,
        run_lifecycle,
        "record_lifecycle_transition",
        run_id="20260820-120000-bnd00003",
    )
    assert payload["reaped"] or payload["skipped"]
    assert (moved / "run.json").is_file()


def test_swap_before_the_shadow_transition_never_writes_outside_the_bound_run(tmp_path, capsys, monkeypatch):
    from brigade import run_shadow

    payload, moved = _reap_under_swap(
        tmp_path,
        capsys,
        monkeypatch,
        run_shadow,
        "record_shadow_comparison",
        run_id="20260820-120000-bnd00004",
    )
    assert payload["reaped"] or payload["skipped"]
    assert (moved / "run.json").is_file()


def test_swap_before_the_terminal_run_json_write_lands_on_the_original_inode(tmp_path, capsys, monkeypatch):
    """The last write in the transaction must still reach the bound inode."""
    repo = _repo(tmp_path)
    run_id = "20260820-120000-bnd00005"
    run_dir = _write_run(repo, run_id, extra=dict(JOURNAL_REQUESTED))
    _write_stale_lock(repo, run_dir)
    outside, outside_json, before = _outside_canary(tmp_path, "outside-terminal")

    real_write = localio.write_text_atomic
    moved: list[Path] = []

    def swap_then_write(path: Path, data: str) -> None:
        if not moved and Path(path).name == "run.json":
            moved.append(_swap_aside(run_dir, outside))
        return real_write(path, data)

    monkeypatch.setattr(localio, "write_text_atomic", swap_then_write)

    assert _reap(repo) == 0
    payload = json.loads(capsys.readouterr().out)
    assert moved, "the terminal run.json write never ran"
    _assert_outside_untouched(outside, outside_json, before)
    assert [row["run_id"] for row in payload["reaped"]] == [run_id]
    # The descriptor-relative write landed on the inode the reaper bound, which
    # the swap moved aside; the symlink target never saw it.
    assert json.loads((moved[0] / "run.json").read_text())["status"] == "orphaned"


def test_swap_during_the_reaper_transaction_before_any_writer_fails_closed(tmp_path, capsys, monkeypatch):
    """A swap that lands between the lock and the binding is a CAS loss."""
    repo = _repo(tmp_path)
    run_dir = _write_run(repo, "20260820-120000-bnd00006", extra=dict(JOURNAL_REQUESTED))
    _write_stale_lock(repo, run_dir)
    outside, outside_json, before = _outside_canary(tmp_path, "outside-transaction")
    moved: list[Path] = []
    real_lock = runguard.run_lock

    def swap_then_lock(*args, **kwargs):
        if not moved:
            moved.append(_swap_aside(run_dir, outside))
        return real_lock(*args, **kwargs)

    monkeypatch.setattr(runguard, "run_lock", swap_then_lock)

    assert _reap(repo) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["reaped"] == []
    assert [row["reason"] for row in payload["skipped"]] == ["concurrently-changed"]
    _assert_outside_untouched(outside, outside_json, before)
    assert json.loads((moved[0] / "run.json").read_text())["status"] == "dispatching"


def test_symlinked_events_directory_fails_closed_and_writes_nothing_outside(tmp_path, capsys):
    repo = _repo(tmp_path)
    run_dir = _write_run(repo, "20260820-120000-bnd00007", extra=dict(JOURNAL_REQUESTED))
    _write_stale_lock(repo, run_dir)
    outside = tmp_path / "outside-events"
    outside.mkdir()
    (run_dir / "events").symlink_to(outside, target_is_directory=True)
    before = (run_dir / "run.json").read_bytes()

    assert _reap(repo) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["reaped"] == []
    assert [row["reason"] for row in payload["skipped"]] == ["write-refused"]
    assert (run_dir / "run.json").read_bytes() == before
    assert list(outside.iterdir()) == []


def test_symlinked_recovery_checkpoints_directory_fails_closed(tmp_path, capsys):
    repo = _repo(tmp_path)
    run_dir = _write_run(repo, "20260820-120000-bnd00008", extra=dict(JOURNAL_REQUESTED))
    _write_stale_lock(repo, run_dir)
    outside = tmp_path / "outside-checkpoints"
    outside.mkdir()
    (run_dir / "events").mkdir()
    (run_dir / "events" / run_checkpoint.CHECKPOINT_DIR_NAME).symlink_to(outside, target_is_directory=True)
    before = (run_dir / "run.json").read_bytes()

    assert _reap(repo) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["reaped"] == []
    assert [row["reason"] for row in payload["skipped"]] == ["write-refused"]
    assert (run_dir / "run.json").read_bytes() == before
    assert list(outside.iterdir()) == []


def test_final_write_oserror_retains_the_claimed_lock_and_writes_nothing_outside(tmp_path, capsys, monkeypatch):
    """A raw OSError from the terminal run.json write must keep the recovery lock."""
    repo = _repo(tmp_path)
    run_dir = _write_run(repo, "20260820-120000-oserror1", extra=dict(JOURNAL_REQUESTED))
    claimed = run_dir.resolve()
    _write_stale_lock(repo, run_dir)
    outside, outside_json, before = _outside_canary(tmp_path, "outside-oserror")
    moved: list[Path] = []
    real_write = localio.write_text_atomic

    def swap_then_oserror(path: Path, data: str) -> None:
        if Path(path).name == "run.json":
            if not moved:
                moved.append(_swap_aside(run_dir, outside))
            raise OSError("final sanctioned write refused")
        return real_write(path, data)

    monkeypatch.setattr(localio, "write_text_atomic", swap_then_oserror)

    assert _reap(repo) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["reaped"] == []
    assert [row["reason"] for row in payload["skipped"]] == ["write-refused"]
    assert moved, "the terminal run.json write never ran"
    _assert_outside_untouched(outside, outside_json, before)
    owner = json.loads((repo / ".brigade" / "run.lock" / "owner.json").read_text())
    assert owner["pid"] == os.getpid()
    assert Path(owner["run_dir"]) == claimed
    assert json.loads((moved[0] / "run.json").read_text())["status"] == "dispatching"


def test_write_refused_under_a_swap_retains_the_claimed_lock(tmp_path, capsys, monkeypatch):
    """Exception cleanup: a refused bound transaction keeps the recovery claim."""
    repo = _repo(tmp_path)
    run_dir = _write_run(repo, "20260820-120000-bnd00009", extra=dict(JOURNAL_REQUESTED))
    _write_stale_lock(repo, run_dir)
    outside, outside_json, before = _outside_canary(tmp_path, "outside-refused")
    moved: list[Path] = []

    def swap_then_refuse(*args, **kwargs):
        if not moved:
            moved.append(_swap_aside(run_dir, outside))
        raise run_checkpoint.CheckpointError("checkpoint refused after swap", category="test")

    monkeypatch.setattr(run_checkpoint, "write_checkpoint", swap_then_refuse)

    assert _reap(repo) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["reaped"] == []
    assert [row["reason"] for row in payload["skipped"]] == ["write-refused"]
    _assert_outside_untouched(outside, outside_json, before)
    owner = json.loads((repo / ".brigade" / "run.lock" / "owner.json").read_text())
    assert owner["pid"] == os.getpid()
    assert json.loads((moved[0] / "run.json").read_text())["status"] == "dispatching"


def _open_descriptor_count() -> int | None:
    try:
        return len(os.listdir("/proc/self/fd"))
    except OSError:
        return None


def test_the_bound_transaction_closes_every_descriptor_on_both_paths(tmp_path, capsys, monkeypatch):
    repo = _repo(tmp_path)
    _write_run(repo, "20260820-120000-bnd00010", extra=dict(JOURNAL_REQUESTED))
    refused = _write_run(repo, "20260820-120000-bnd00011", extra=dict(JOURNAL_REQUESTED))
    _write_stale_lock(repo, refused)

    baseline = _open_descriptor_count()
    if baseline is None:
        pytest.skip("descriptor accounting needs /proc/self/fd")

    assert _reap(repo) == 0
    capsys.readouterr()
    assert _open_descriptor_count() == baseline

    _write_stale_lock(repo, refused)

    def refuse(*args, **kwargs):
        raise run_checkpoint.CheckpointError("checkpoint refused in test", category="test")

    monkeypatch.setattr(run_checkpoint, "write_checkpoint", refuse)
    assert _reap(repo) == 0
    capsys.readouterr()
    assert _open_descriptor_count() == baseline


def test_no_binding_parity_for_ordinary_run_json_writers(tmp_path):
    """Ordinary callers never activate a binding and keep pathname behavior."""
    from brigade import aboyeur, receipt_schema

    repo = _repo(tmp_path)
    run_dir = _write_run(repo, "20260820-120000-bnd00012", extra=dict(JOURNAL_REQUESTED))
    run_json = run_dir / "run.json"
    assert run_dirfd.active_binding_for(run_json) is None

    payload = json.loads(run_json.read_text())
    payload["status"] = "planning"
    with runguard.run_lock(repo, run_dir=run_dir, wait_seconds=0):
        aboyeur._write_json(run_json, receipt_schema.stamp_run_receipt(payload))
    assert run_dirfd.active_binding_for(run_json) is None
    assert json.loads(run_json.read_text())["status"] == "planning"
    assert (run_dir / "events" / "lifecycle.jsonl").is_file()


def test_bound_helpers_match_pathname_helpers_without_a_binding(tmp_path):
    events = tmp_path / "events"
    events.mkdir()
    present = events / "lifecycle.jsonl"
    present.write_bytes(b"payload\n")
    missing = events / "absent.jsonl"
    link = events / "link.jsonl"
    link.symlink_to(present)

    assert run_journal.bound_is_file(present) is present.is_file()
    assert run_journal.bound_is_file(missing) is missing.is_file()
    assert run_journal.bound_is_file(link) is link.is_file()
    assert run_journal.bound_is_dir(events) is events.is_dir()
    assert run_journal.bound_exists(link) is link.exists()
    assert run_journal.bound_lexists(link) is os.path.lexists(link)
    assert run_journal.bound_read_bytes(present) == present.read_bytes()
    assert run_journal.bound_read_bytes(link) == present.read_bytes()
    assert run_journal.bound_lstat(present).st_ino == present.lstat().st_ino
    assert run_journal.bound_lstat(missing) is None
    assert run_journal.normalize_run_dir(tmp_path) == tmp_path.expanduser().resolve()


def test_bound_helpers_stay_on_the_bound_inode_after_a_swap(tmp_path):
    """A swap after bind cannot redirect a bound read, stat, or normalization."""
    from brigade import run_dirfd as dirfd_module

    run_dir = tmp_path / "run"
    (run_dir / "events").mkdir(parents=True)
    (run_dir / "run.json").write_bytes(b'{"status": "dispatching"}\n')
    (run_dir / "events" / "lifecycle.jsonl").write_bytes(b"bound\n")

    outside = tmp_path / "outside"
    (outside / "events").mkdir(parents=True)
    (outside / "run.json").write_bytes(b'{"status": "outside"}\n')
    (outside / "events" / "lifecycle.jsonl").write_bytes(b"outside\n")

    with dirfd_module.bound_run_dir(run_dir) as bound:
        moved = _swap_aside(run_dir, outside)
        assert run_journal.normalize_run_dir(run_dir) == bound.path
        assert run_journal.bound_read_bytes(run_dir / "run.json") == b'{"status": "dispatching"}\n'
        assert run_journal.bound_read_bytes(run_dir / "events" / "lifecycle.jsonl") == b"bound\n"
        assert run_journal.bound_is_file(run_dir / "events" / "lifecycle.jsonl")
        assert run_journal.bound_lstat(run_dir / "run.json") is not None
    assert (moved / "run.json").read_bytes() == b'{"status": "dispatching"}\n'
    assert (outside / "run.json").read_bytes() == b'{"status": "outside"}\n'


def test_a_symlinked_journal_inside_a_binding_is_refused_not_followed(tmp_path):
    from brigade import run_dirfd as dirfd_module

    run_dir = tmp_path / "run"
    (run_dir / "events").mkdir(parents=True)
    outside = tmp_path / "outside.jsonl"
    outside.write_bytes(b"outside\n")
    journal = run_dir / "events" / "lifecycle.jsonl"
    journal.symlink_to(outside)

    with dirfd_module.bound_run_dir(run_dir):
        assert run_journal.bound_is_file(journal) is False
        with pytest.raises(run_journal.RunJournalError) as excinfo:
            run_journal.ensure_journal(journal)
    assert "lifecycle.jsonl" in excinfo.value.diagnostic
    assert outside.read_bytes() == b"outside\n"


def test_swap_before_the_sanctioned_writer_poisons_no_write_outside_the_run(tmp_path, capsys, monkeypatch):
    """The swap lands between the reaper's CAS read and ``aboyeur._write_json``.

    Bound reads and shadow reads are descriptor-aware, so a swap here cannot
    redirect those gates to outside bytes. Every write in the transaction is
    descriptor-bound: the outside directory is never mutated and the run is
    never silently terminalized somewhere else.
    """
    from brigade import receipt_schema

    repo = _repo(tmp_path)
    run_dir = _write_run(repo, "20260820-120000-bnd00013", extra=dict(JOURNAL_REQUESTED))
    _write_stale_lock(repo, run_dir)
    outside, outside_json, before = _outside_canary(tmp_path, "outside-poison")
    real_stamp = receipt_schema.stamp_run_receipt
    moved: list[Path] = []

    def swap_then_stamp(payload):
        if not moved:
            moved.append(_swap_aside(run_dir, outside))
        return real_stamp(payload)

    monkeypatch.setattr(receipt_schema, "stamp_run_receipt", swap_then_stamp)

    assert _reap(repo) == 0
    payload = json.loads(capsys.readouterr().out)
    assert moved, "the sanctioned writer never ran"
    _assert_outside_untouched(outside, outside_json, before)
    written = json.loads((moved[0] / "run.json").read_text())
    if payload["reaped"]:
        assert written["status"] == "orphaned"
    else:
        assert written["status"] == "dispatching"
