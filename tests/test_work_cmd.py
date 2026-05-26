import json
import subprocess
from datetime import datetime, timezone

from brigade import cli
from brigade import dogfood_cmd
from brigade import work_cmd


def _write_json(path, payload):
    path.write_text(json.dumps(payload, indent=2) + "\n")


def _init_git_repo(path):
    subprocess.run(["git", "init"], cwd=path, check=True, stdout=subprocess.DEVNULL)


def test_work_status_reports_repo_and_dogfood_state(tmp_path, monkeypatch, capsys):
    _init_git_repo(tmp_path)
    (tmp_path / "changed.txt").write_text("work\n")
    dogfood_cmd.init(target=tmp_path, timeout_seconds=33)
    run_dir = tmp_path / ".brigade" / "runs" / "latest"
    run_dir.mkdir(parents=True)
    _write_json(
        run_dir / "run.json",
        {
            "started_at": "2026-05-26T12:00:00Z",
            "status": "ok",
            "task": "review current work",
        },
    )
    (run_dir / "final.txt").write_text("Done.\n\nNext step: Build work start.\n")
    monkeypatch.setattr(work_cmd.shutil, "which", lambda name: f"/usr/bin/{name}")

    assert work_cmd.status(target=tmp_path) == 0
    out = capsys.readouterr().out
    assert f"work: {tmp_path.resolve()}" in out
    assert "repo:" in out
    assert "branch:" in out
    assert "dirty_files:" in out
    assert "?? changed.txt" in out
    assert "dogfood: ready" in out
    assert f"dogfood_config: {tmp_path / '.brigade' / 'dogfood.toml'}" in out
    assert "codex: /usr/bin/codex" in out
    assert "latest_run: 2026-05-26T12:00:00Z [ok]" in out
    assert "latest_task: review current work" in out
    assert "next: Build work start." in out
    assert "next_command: brigade dogfood next" in out


def test_work_status_runs_without_dogfood_config(tmp_path, monkeypatch, capsys):
    _init_git_repo(tmp_path)
    monkeypatch.setattr(work_cmd.shutil, "which", lambda name: None)

    assert work_cmd.status(target=tmp_path) == 0
    out = capsys.readouterr().out
    assert "dogfood: not ready" in out
    assert "dogfood_config:" in out
    assert "(missing)" in out
    assert "codex: missing" in out
    assert "latest_run: none" in out
    assert "next: none" in out


def test_work_status_rejects_bad_limit(tmp_path, capsys):
    tmp_path.mkdir(exist_ok=True)

    assert work_cmd.status(target=tmp_path, limit=0) == 2
    assert "--limit must be a positive integer" in capsys.readouterr().err


def test_work_doctor_reports_ready_repo(tmp_path, monkeypatch, capsys):
    _init_git_repo(tmp_path)
    dogfood_cmd.init(target=tmp_path)
    run_dir = tmp_path / ".brigade" / "runs" / "latest"
    run_dir.mkdir(parents=True)
    _write_json(run_dir / "run.json", {"started_at": "2026-05-26T12:00:00Z", "status": "ok", "task": "review"})
    (run_dir / "final.txt").write_text("Done.\n\nNext step: Build doctor.\n")
    monkeypatch.setattr(work_cmd.shutil, "which", lambda name: f"/usr/bin/{name}" if name == "codex" else None)
    monkeypatch.setattr(dogfood_cmd, "_check_git_ignored", lambda repo, path: "yes")

    assert work_cmd.doctor(target=tmp_path) == 0
    out = capsys.readouterr().out
    assert "work doctor:" in out
    assert "[ok] target:" in out
    assert "[ok] git:" in out
    assert "[ok] dogfood_config:" in out
    assert "[ok] codex: /usr/bin/codex" in out
    assert "[ok] latest_next: Build doctor." in out
    assert "[ok] ready: daily work loop is usable" in out


def test_work_doctor_reports_blockers(tmp_path, monkeypatch, capsys):
    _init_git_repo(tmp_path)
    monkeypatch.setattr(work_cmd.shutil, "which", lambda name: None)

    assert work_cmd.doctor(target=tmp_path) == 1
    out = capsys.readouterr().out
    assert "[fail] dogfood_config:" in out
    assert "brigade dogfood init" in out
    assert "[fail] codex: missing on PATH" in out
    assert "[fail] ready: 2 blockers" in out


def test_work_doctor_rejects_missing_target(tmp_path, capsys):
    assert work_cmd.doctor(target=tmp_path / "missing") == 2
    assert "not a directory" in capsys.readouterr().out


def test_work_start_creates_active_session(tmp_path, monkeypatch, capsys):
    _init_git_repo(tmp_path)
    monkeypatch.setattr(
        work_cmd,
        "_now",
        lambda: datetime(2026, 5, 26, 12, 0, 0, tzinfo=timezone.utc),
    )

    assert work_cmd.start(target=tmp_path, title="Build Work Loop") == 0
    out = capsys.readouterr().out
    session_dir = tmp_path / ".brigade" / "work" / "20260526-120000-build-work-loop"
    assert f"session: {session_dir}" in out
    assert (tmp_path / ".brigade" / "work" / "current").read_text() == "20260526-120000-build-work-loop\n"
    payload = json.loads((session_dir / "session.json").read_text())
    assert payload["status"] == "active"
    assert payload["title"] == "Build Work Loop"
    assert payload["start"]["git"]["available"] is True
    assert (session_dir / "start.md").is_file()


def test_work_start_refuses_existing_session_without_force(tmp_path, monkeypatch, capsys):
    _init_git_repo(tmp_path)
    monkeypatch.setattr(
        work_cmd,
        "_now",
        lambda: datetime(2026, 5, 26, 12, 0, 0, tzinfo=timezone.utc),
    )

    assert work_cmd.start(target=tmp_path, title="one") == 0
    assert work_cmd.start(target=tmp_path, title="two") == 2
    assert "already active" in capsys.readouterr().err


def test_work_note_appends_to_active_session(tmp_path, monkeypatch, capsys):
    _init_git_repo(tmp_path)
    times = iter(
        [
            datetime(2026, 5, 26, 12, 0, 0, tzinfo=timezone.utc),
            datetime(2026, 5, 26, 12, 30, 0, tzinfo=timezone.utc),
            datetime(2026, 5, 26, 12, 45, 0, tzinfo=timezone.utc),
        ]
    )
    monkeypatch.setattr(work_cmd, "_now", lambda: next(times))
    assert work_cmd.start(target=tmp_path, title="Build Work Loop") == 0

    assert work_cmd.note(target=tmp_path, text="wired parser") == 0
    assert work_cmd.note(target=tmp_path, text="added tests") == 0
    out = capsys.readouterr().out
    assert "note: wired parser" in out
    assert "note: added tests" in out
    session_dir = tmp_path / ".brigade" / "work" / "20260526-120000-build-work-loop"
    payload = json.loads((session_dir / "session.json").read_text())
    assert payload["status"] == "active"
    assert payload["notes"] == [
        {"created_at": "2026-05-26T12:30:00+00:00", "text": "wired parser"},
        {"created_at": "2026-05-26T12:45:00+00:00", "text": "added tests"},
    ]
    notes = (session_dir / "notes.md").read_text()
    assert "# Brigade Work Session Notes" in notes
    assert "wired parser" in notes
    assert "added tests" in notes


def test_work_note_reports_no_active_session(tmp_path, capsys):
    _init_git_repo(tmp_path)

    assert work_cmd.note(target=tmp_path, text="checkpoint") == 1
    assert "no active work session" in capsys.readouterr().err


def test_work_note_rejects_empty_note(tmp_path, capsys):
    _init_git_repo(tmp_path)

    assert work_cmd.note(target=tmp_path, text="  ") == 2
    assert "note text is required" in capsys.readouterr().err


def test_work_end_closes_active_session(tmp_path, monkeypatch, capsys):
    _init_git_repo(tmp_path)
    times = iter(
        [
            datetime(2026, 5, 26, 12, 0, 0, tzinfo=timezone.utc),
            datetime(2026, 5, 26, 13, 0, 0, tzinfo=timezone.utc),
        ]
    )
    monkeypatch.setattr(work_cmd, "_now", lambda: next(times))
    assert work_cmd.start(target=tmp_path, title="Build Work Loop") == 0

    assert work_cmd.end(target=tmp_path, note="done for now") == 0
    out = capsys.readouterr().out
    session_dir = tmp_path / ".brigade" / "work" / "20260526-120000-build-work-loop"
    assert f"session: {session_dir}" in out
    assert not (tmp_path / ".brigade" / "work" / "current").exists()
    payload = json.loads((session_dir / "session.json").read_text())
    assert payload["status"] == "ended"
    assert payload["note"] == "done for now"
    assert payload["ended_at"] == "2026-05-26T13:00:00+00:00"
    assert payload["end"]["git"]["available"] is True
    assert "done for now" in (session_dir / "end.md").read_text()


def test_work_end_can_write_handoff(tmp_path, monkeypatch, capsys):
    _init_git_repo(tmp_path)
    times = iter(
        [
            datetime(2026, 5, 26, 12, 0, 0, tzinfo=timezone.utc),
            datetime(2026, 5, 26, 13, 0, 0, tzinfo=timezone.utc),
        ]
    )
    monkeypatch.setattr(work_cmd, "_now", lambda: next(times))
    assert work_cmd.start(target=tmp_path, title="Build Work Loop") == 0

    inbox = tmp_path / "handoffs"
    assert work_cmd.end(target=tmp_path, note="done for now", handoff=True, handoff_inbox=inbox) == 0
    out = capsys.readouterr().out
    assert "handoff:" in out
    handoffs = list(inbox.glob("*-brigade-work-build-work-loop-*.md"))
    assert len(handoffs) == 1
    handoff = handoffs[0].read_text()
    assert "# Memory Handoff" in handoff
    assert "Brigade work session ended" in handoff
    assert "done for now" in handoff
    session_dir = tmp_path / ".brigade" / "work" / "20260526-120000-build-work-loop"
    payload = json.loads((session_dir / "session.json").read_text())
    assert payload["handoff"] == str(handoffs[0])


def test_work_end_defaults_handoff_to_codex_inbox(tmp_path, monkeypatch):
    _init_git_repo(tmp_path)
    times = iter(
        [
            datetime(2026, 5, 26, 12, 0, 0, tzinfo=timezone.utc),
            datetime(2026, 5, 26, 13, 0, 0, tzinfo=timezone.utc),
        ]
    )
    monkeypatch.setattr(work_cmd, "_now", lambda: next(times))
    assert work_cmd.start(target=tmp_path, title="Build Work Loop") == 0

    assert work_cmd.end(target=tmp_path, note="done for now", handoff=True) == 0
    session_dir = tmp_path / ".brigade" / "work" / "20260526-120000-build-work-loop"
    payload = json.loads((session_dir / "session.json").read_text())
    assert payload["handoff"].startswith(str(tmp_path / ".codex" / "memory-handoffs"))


def test_work_end_reports_no_active_session(tmp_path, capsys):
    _init_git_repo(tmp_path)

    assert work_cmd.end(target=tmp_path) == 1
    assert "no active work session" in capsys.readouterr().err


def test_work_list_prints_recent_sessions(tmp_path, monkeypatch, capsys):
    _init_git_repo(tmp_path)
    times = iter(
        [
            datetime(2026, 5, 26, 12, 0, 0, tzinfo=timezone.utc),
            datetime(2026, 5, 26, 12, 30, 0, tzinfo=timezone.utc),
            datetime(2026, 5, 26, 13, 0, 0, tzinfo=timezone.utc),
            datetime(2026, 5, 26, 13, 30, 0, tzinfo=timezone.utc),
        ]
    )
    monkeypatch.setattr(work_cmd, "_now", lambda: next(times))
    assert work_cmd.start(target=tmp_path, title="Older Session") == 0
    assert work_cmd.end(target=tmp_path) == 0
    assert work_cmd.start(target=tmp_path, title="Newer Session") == 0
    assert work_cmd.end(target=tmp_path) == 0

    assert work_cmd.list_sessions(target=tmp_path, limit=10) == 0
    out = capsys.readouterr().out
    assert out.index("Newer Session") < out.index("Older Session")
    assert "[ended]" in out
    assert "dirty=" in out


def test_work_latest_shows_latest_session(tmp_path, monkeypatch, capsys):
    _init_git_repo(tmp_path)
    times = iter(
        [
            datetime(2026, 5, 26, 12, 0, 0, tzinfo=timezone.utc),
            datetime(2026, 5, 26, 13, 0, 0, tzinfo=timezone.utc),
        ]
    )
    monkeypatch.setattr(work_cmd, "_now", lambda: next(times))
    assert work_cmd.start(target=tmp_path, title="Build Work Loop") == 0
    assert work_cmd.end(target=tmp_path, note="done") == 0

    assert work_cmd.latest(target=tmp_path) == 0
    out = capsys.readouterr().out
    assert "session:" in out
    assert "title: Build Work Loop" in out
    assert "status: ended" in out
    assert "note: done" in out
    assert "git:" in out
    assert "dogfood:" in out


def test_work_show_accepts_session_id(tmp_path, monkeypatch, capsys):
    _init_git_repo(tmp_path)
    monkeypatch.setattr(
        work_cmd,
        "_now",
        lambda: datetime(2026, 5, 26, 12, 0, 0, tzinfo=timezone.utc),
    )
    assert work_cmd.start(target=tmp_path, title="Build Work Loop") == 0

    assert work_cmd.show(target=tmp_path, session="20260526-120000-build-work-loop") == 0
    out = capsys.readouterr().out
    assert "id: 20260526-120000-build-work-loop" in out
    assert "status: active" in out


def test_work_latest_reports_no_sessions(tmp_path, capsys):
    _init_git_repo(tmp_path)

    assert work_cmd.latest(target=tmp_path) == 1
    assert "no work sessions found" in capsys.readouterr().err


def test_work_recap_summarizes_recent_sessions(tmp_path, monkeypatch, capsys):
    _init_git_repo(tmp_path)
    times = iter(
        [
            datetime(2026, 5, 25, 12, 0, 0, tzinfo=timezone.utc),
            datetime(2026, 5, 25, 13, 0, 0, tzinfo=timezone.utc),
            datetime(2026, 5, 26, 12, 0, 0, tzinfo=timezone.utc),
            datetime(2026, 5, 26, 13, 0, 0, tzinfo=timezone.utc),
        ]
    )
    monkeypatch.setattr(work_cmd, "_now", lambda: next(times))
    dogfood_cmd.init(target=tmp_path)
    run_dir = tmp_path / ".brigade" / "runs" / "latest"
    run_dir.mkdir(parents=True)
    _write_json(run_dir / "run.json", {"started_at": "2026-05-26T11:00:00Z", "status": "ok", "task": "review"})
    (run_dir / "final.txt").write_text("Done.\n\nNext step: Build recap.\n")

    assert work_cmd.start(target=tmp_path, title="Older Session") == 0
    assert work_cmd.end(target=tmp_path, note="old note") == 0
    assert work_cmd.start(target=tmp_path, title="Newer Session") == 0
    assert work_cmd.end(target=tmp_path, note="new note", handoff=True, handoff_inbox=tmp_path / "handoffs") == 0

    assert work_cmd.recap(target=tmp_path, since="2026-05-26", limit=5) == 0
    out = capsys.readouterr().out
    assert "work recap:" in out
    assert "since: 2026-05-26" in out
    assert "sessions: 1" in out
    assert "branches:" in out
    assert "handoffs: 1" in out
    assert "Newer Session" in out
    assert "Older Session" not in out
    assert "note: new note" in out
    assert "next: Build recap." in out


def test_work_recap_rejects_bad_since(tmp_path, capsys):
    _init_git_repo(tmp_path)

    assert work_cmd.recap(target=tmp_path, since="05-26-2026") == 2
    assert "--since must use YYYY-MM-DD" in capsys.readouterr().err


def test_work_resume_reports_active_session(tmp_path, monkeypatch, capsys):
    _init_git_repo(tmp_path)
    dogfood_cmd.init(target=tmp_path)
    run_dir = tmp_path / ".brigade" / "runs" / "latest"
    run_dir.mkdir(parents=True)
    _write_json(run_dir / "run.json", {"started_at": "2026-05-26T12:10:00Z", "status": "ok", "task": "review"})
    (run_dir / "final.txt").write_text("Done.\n\nNext step: Build resume.\n")
    monkeypatch.setattr(
        work_cmd,
        "_now",
        lambda: datetime(2026, 5, 26, 12, 0, 0, tzinfo=timezone.utc),
    )
    assert work_cmd.start(target=tmp_path, title="Active Work") == 0

    assert work_cmd.resume(target=tmp_path) == 0
    out = capsys.readouterr().out
    assert "work resume:" in out
    assert "active_session:" in out
    assert "active_session_title: Active Work" in out
    assert "latest_run: 2026-05-26T12:10:00Z [ok]" in out
    assert "next: Build resume." in out
    assert 'suggested_command: brigade work end --note "..." --handoff' in out


def test_work_resume_suggests_work_run_from_latest_next(tmp_path, monkeypatch, capsys):
    _init_git_repo(tmp_path)
    dogfood_cmd.init(target=tmp_path)
    run_dir = tmp_path / ".brigade" / "runs" / "latest"
    run_dir.mkdir(parents=True)
    _write_json(run_dir / "run.json", {"started_at": "2026-05-26T12:10:00Z", "status": "ok", "task": "review"})
    (run_dir / "final.txt").write_text("Done.\n\nNext step: Build resume.\n")
    times = iter(
        [
            datetime(2026, 5, 26, 12, 0, 0, tzinfo=timezone.utc),
            datetime(2026, 5, 26, 13, 0, 0, tzinfo=timezone.utc),
        ]
    )
    monkeypatch.setattr(work_cmd, "_now", lambda: next(times))
    assert work_cmd.start(target=tmp_path, title="Ended Work") == 0
    assert work_cmd.end(target=tmp_path, note="done", handoff=True, handoff_inbox=tmp_path / "handoffs") == 0

    assert work_cmd.resume(target=tmp_path) == 0
    out = capsys.readouterr().out
    assert "active_session: none" in out
    assert "latest_session:" in out
    assert "latest_session_title: Ended Work" in out
    assert "latest_session_handoff:" in out
    assert "next: Build resume." in out
    assert "suggested_command: brigade work run 'Build resume.'" in out


def test_work_next_reports_latest_next_as_default_task(tmp_path, monkeypatch, capsys):
    _init_git_repo(tmp_path)
    dogfood_cmd.init(target=tmp_path)
    run_dir = tmp_path / ".brigade" / "runs" / "latest"
    run_dir.mkdir(parents=True)
    _write_json(run_dir / "run.json", {"started_at": "2026-05-26T12:10:00Z", "status": "ok", "task": "review"})
    (run_dir / "final.txt").write_text("Done.\n\nNext step: Build next command.\n")
    monkeypatch.setattr(work_cmd.shutil, "which", lambda name: f"/usr/bin/{name}")

    assert work_cmd.next(target=tmp_path) == 0
    out = capsys.readouterr().out
    assert "work next:" in out
    assert "active_session: none" in out
    assert "dogfood_ready: True" in out
    assert "latest_run: 2026-05-26T12:10:00Z [ok]" in out
    assert "next_source: latest_dogfood_run" in out
    assert "next: Build next command." in out
    assert "suggested_command: brigade work run" in out


def test_work_next_json_reports_resolved_task(tmp_path, monkeypatch, capsys):
    _init_git_repo(tmp_path)
    dogfood_cmd.init(target=tmp_path)
    run_dir = tmp_path / ".brigade" / "runs" / "latest"
    run_dir.mkdir(parents=True)
    _write_json(run_dir / "run.json", {"started_at": "2026-05-26T12:10:00Z", "status": "ok", "task": "review"})
    (run_dir / "final.txt").write_text("Done.\n\nNext step: Build JSON output.\n")
    monkeypatch.setattr(work_cmd.shutil, "which", lambda name: f"/usr/bin/{name}")
    capsys.readouterr()

    assert work_cmd.next(target=tmp_path, json_output=True) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["target"] == str(tmp_path.resolve())
    assert payload["active_session"] is None
    assert payload["dogfood"]["ready"] is True
    assert payload["dogfood"]["latest_run"]["status"] == "ok"
    assert payload["next_source"] == "latest_dogfood_run"
    assert payload["next"] == "Build JSON output."
    assert payload["suggested_command"] == "brigade work run"


def test_work_next_falls_back_to_default_review(tmp_path, capsys):
    _init_git_repo(tmp_path)

    assert work_cmd.next(target=tmp_path) == 0
    out = capsys.readouterr().out
    assert "dogfood_ready: False" in out
    assert "latest_run: none" in out
    assert "next_source: default_review" in out
    assert f"next: {dogfood_cmd.DEFAULT_TASK}" in out


def test_work_bootstrap_prepares_daily_loop(tmp_path, monkeypatch, capsys):
    _init_git_repo(tmp_path)
    monkeypatch.setattr(work_cmd.shutil, "which", lambda name: f"/usr/bin/{name}" if name == "codex" else None)

    assert work_cmd.bootstrap(target=tmp_path, timeout_seconds=44) == 0
    out = capsys.readouterr().out
    assert "work bootstrap:" in out
    assert "[ok] dogfood_config:" in out
    assert "[ok] gitignore:" in out
    assert "[ok] ready: daily work loop is usable" in out
    assert "next_command: brigade work run" in out
    assert (tmp_path / ".brigade" / "dogfood.toml").is_file()
    assert (tmp_path / ".brigade" / "runs").is_dir()
    assert (tmp_path / ".brigade" / "work").is_dir()
    assert (tmp_path / ".codex" / "memory-handoffs").is_dir()
    gitignore = (tmp_path / ".gitignore").read_text()
    assert ".brigade/dogfood.toml" in gitignore
    assert ".brigade/runs/" in gitignore
    assert ".brigade/work/" in gitignore
    assert ".codex/memory-handoffs/*" in gitignore
    config = (tmp_path / ".brigade" / "dogfood.toml").read_text()
    assert "timeout_seconds = 44" in config


def test_work_bootstrap_preserves_existing_config_without_force(tmp_path, monkeypatch, capsys):
    _init_git_repo(tmp_path)
    dogfood_cmd.init(
        target=tmp_path,
        handoff_inbox=tmp_path / ".claude" / "memory-handoffs",
        timeout_seconds=12,
    )
    monkeypatch.setattr(work_cmd.shutil, "which", lambda name: f"/usr/bin/{name}" if name == "codex" else None)

    assert work_cmd.bootstrap(target=tmp_path, timeout_seconds=44) == 0
    out = capsys.readouterr().out
    assert "exists at" in out
    config = (tmp_path / ".brigade" / "dogfood.toml").read_text()
    assert "timeout_seconds = 12" in config
    gitignore = (tmp_path / ".gitignore").read_text()
    assert ".claude/memory-handoffs/*" in gitignore
    assert ".codex/memory-handoffs/*" not in gitignore


def test_work_bootstrap_reports_missing_codex(tmp_path, monkeypatch, capsys):
    _init_git_repo(tmp_path)
    monkeypatch.setattr(work_cmd.shutil, "which", lambda name: None)

    assert work_cmd.bootstrap(target=tmp_path) == 1
    out = capsys.readouterr().out
    assert "[fail] codex: missing on PATH" in out
    assert "[fail] ready: 1 blocker" in out


def test_work_resume_empty_state(tmp_path, capsys):
    _init_git_repo(tmp_path)

    assert work_cmd.resume(target=tmp_path) == 0
    out = capsys.readouterr().out
    assert "active_session: none" in out
    assert "latest_session: none" in out
    assert "latest_run: none" in out
    assert "next: none" in out
    assert "suggested_command: brigade work run" in out


def test_work_run_wraps_dogfood_session(tmp_path, monkeypatch, capsys):
    _init_git_repo(tmp_path)
    artifacts_dir = tmp_path / ".brigade" / "runs"
    dogfood_cmd.init(target=tmp_path, artifacts_dir=artifacts_dir)
    times = iter(
        [
            datetime(2026, 5, 26, 12, 0, 0, tzinfo=timezone.utc),
            datetime(2026, 5, 26, 13, 0, 0, tzinfo=timezone.utc),
        ]
    )
    monkeypatch.setattr(work_cmd, "_now", lambda: next(times))
    seen = {}

    def fake_dogfood_run(task, **kwargs):
        seen["task"] = task
        seen.update(kwargs)
        run_dir = kwargs["output_dir"]
        run_dir.mkdir(parents=True)
        _write_json(
            run_dir / "run.json",
            {"started_at": "2026-05-26T12:10:00Z", "status": "ok", "task": task},
        )
        (run_dir / "final.txt").write_text("Done.\n\nNext step: Build work run.\n")
        return 0

    monkeypatch.setattr(dogfood_cmd, "run", fake_dogfood_run)
    run_dir = artifacts_dir / "work-run"

    assert (
        work_cmd.run(
            "review the repo",
            target=tmp_path,
            title="Daily Review",
            output_dir=run_dir,
            handoff_inbox=tmp_path / "handoffs",
        )
        == 0
    )
    assert seen["task"] == "review the repo"
    assert seen["target"] == tmp_path.resolve()
    assert seen["output_dir"] == run_dir
    assert seen["handoff"] is False
    assert seen["handoff_inbox"] is None
    assert seen["inspect"] is True
    assert not (tmp_path / ".brigade" / "work" / "current").exists()
    session_dir = tmp_path / ".brigade" / "work" / "20260526-120000-daily-review"
    payload = json.loads((session_dir / "session.json").read_text())
    assert payload["status"] == "ended"
    assert payload["note"] == "brigade work run completed with dogfood exit code 0"
    assert payload["end"]["dogfood"]["latest_run"]["path"] == str(run_dir)
    assert payload["end"]["dogfood"]["next"] == "Build work run."
    assert "handoff" in payload
    out = capsys.readouterr().out
    assert "work recap:" in out
    assert "Daily Review" in out
    assert "next: Build work run." in out


def test_work_run_uses_latest_next_when_task_is_omitted(tmp_path, monkeypatch):
    _init_git_repo(tmp_path)
    artifacts_dir = tmp_path / ".brigade" / "runs"
    dogfood_cmd.init(target=tmp_path, artifacts_dir=artifacts_dir)
    latest_dir = artifacts_dir / "latest"
    latest_dir.mkdir(parents=True)
    _write_json(
        latest_dir / "run.json",
        {"started_at": "2026-05-26T11:00:00Z", "status": "ok", "task": "review"},
    )
    (latest_dir / "final.txt").write_text("Done.\n\nNext step: Build consumed task.\n")
    times = iter(
        [
            datetime(2026, 5, 26, 12, 0, 0, tzinfo=timezone.utc),
            datetime(2026, 5, 26, 13, 0, 0, tzinfo=timezone.utc),
        ]
    )
    monkeypatch.setattr(work_cmd, "_now", lambda: next(times))
    seen = {}

    def fake_dogfood_run(task, **kwargs):
        seen["task"] = task
        run_dir = kwargs["output_dir"]
        run_dir.mkdir(parents=True)
        _write_json(
            run_dir / "run.json",
            {"started_at": "2026-05-26T12:10:00Z", "status": "ok", "task": task},
        )
        (run_dir / "final.txt").write_text("Done.\n\nNext step: Build follow-up.\n")
        return 0

    monkeypatch.setattr(dogfood_cmd, "run", fake_dogfood_run)

    assert work_cmd.run(None, target=tmp_path, output_dir=artifacts_dir / "new", handoff=False) == 0
    assert seen["task"] == "Build consumed task."
    session_dir = tmp_path / ".brigade" / "work" / "20260526-120000-build-consumed-task"
    payload = json.loads((session_dir / "session.json").read_text())
    assert payload["title"] == "Build consumed task."


def test_work_run_closes_session_when_dogfood_fails(tmp_path, monkeypatch):
    _init_git_repo(tmp_path)
    dogfood_cmd.init(target=tmp_path)
    times = iter(
        [
            datetime(2026, 5, 26, 12, 0, 0, tzinfo=timezone.utc),
            datetime(2026, 5, 26, 13, 0, 0, tzinfo=timezone.utc),
        ]
    )
    monkeypatch.setattr(work_cmd, "_now", lambda: next(times))
    monkeypatch.setattr(dogfood_cmd, "run", lambda task, **kwargs: 7)

    assert work_cmd.run("review the repo", target=tmp_path, handoff=False) == 7
    assert not (tmp_path / ".brigade" / "work" / "current").exists()
    session_dir = tmp_path / ".brigade" / "work" / "20260526-120000-review-the-repo"
    payload = json.loads((session_dir / "session.json").read_text())
    assert payload["status"] == "ended"
    assert payload["note"] == "brigade work run completed with dogfood exit code 7"
    assert "handoff" not in payload


def test_work_run_rejects_bad_recap_limit(tmp_path, capsys):
    _init_git_repo(tmp_path)

    assert work_cmd.run(None, target=tmp_path, recap_limit=0) == 2
    assert "--recap-limit must be a positive integer" in capsys.readouterr().err


def test_work_status_cli(tmp_path, monkeypatch):
    seen = {}

    def fake_status(**kwargs):
        seen.update(kwargs)
        return 0

    monkeypatch.setattr(work_cmd, "status", fake_status)

    assert cli.main(["work", "status", "--target", str(tmp_path), "--limit", "3"]) == 0
    assert seen == {"target": tmp_path, "limit": 3}


def test_work_resume_cli(tmp_path, monkeypatch):
    seen = {}

    def fake_resume(**kwargs):
        seen.update(kwargs)
        return 0

    monkeypatch.setattr(work_cmd, "resume", fake_resume)

    assert cli.main(["work", "resume", "--target", str(tmp_path)]) == 0
    assert seen == {"target": tmp_path}


def test_work_next_cli(tmp_path, monkeypatch):
    seen = {}

    def fake_next(**kwargs):
        seen.update(kwargs)
        return 0

    monkeypatch.setattr(work_cmd, "next", fake_next)

    assert cli.main(["work", "next", "--target", str(tmp_path)]) == 0
    assert seen == {"target": tmp_path, "json_output": False}
    seen.clear()
    assert cli.main(["work", "next", "--target", str(tmp_path), "--json"]) == 0
    assert seen == {"target": tmp_path, "json_output": True}


def test_work_bootstrap_cli(tmp_path, monkeypatch):
    seen = {}

    def fake_bootstrap(**kwargs):
        seen.update(kwargs)
        return 0

    monkeypatch.setattr(work_cmd, "bootstrap", fake_bootstrap)

    assert (
        cli.main(
            [
                "work",
                "bootstrap",
                "--target",
                str(tmp_path),
                "--artifacts-dir",
                str(tmp_path / "runs"),
                "--handoff-inbox",
                str(tmp_path / "handoffs"),
                "--force",
                "--no-handoff",
                "--no-inspect",
                "--native-read-only-sandbox",
                "--timeout-seconds",
                "55",
                "--no-gitignore",
            ]
        )
        == 0
    )
    assert seen == {
        "target": tmp_path,
        "artifacts_dir": tmp_path / "runs",
        "handoff_inbox": tmp_path / "handoffs",
        "force": True,
        "handoff": False,
        "inspect": False,
        "native_read_only_sandbox": True,
        "timeout_seconds": 55.0,
        "update_gitignore": False,
    }


def test_work_doctor_cli(tmp_path, monkeypatch):
    seen = {}

    def fake_doctor(**kwargs):
        seen.update(kwargs)
        return 0

    monkeypatch.setattr(work_cmd, "doctor", fake_doctor)

    assert cli.main(["work", "doctor", "--target", str(tmp_path)]) == 0
    assert seen == {"target": tmp_path}


def test_work_start_and_end_cli(tmp_path, monkeypatch):
    seen = []

    def fake_start(**kwargs):
        seen.append(("start", kwargs))
        return 0

    def fake_end(**kwargs):
        seen.append(("end", kwargs))
        return 0

    monkeypatch.setattr(work_cmd, "start", fake_start)
    monkeypatch.setattr(work_cmd, "end", fake_end)

    assert cli.main(["work", "start", "Build", "Loop", "--target", str(tmp_path), "--force"]) == 0
    assert (
        cli.main(
            [
                "work",
                "end",
                "--target",
                str(tmp_path),
                "--note",
                "done",
                "--handoff",
                "--handoff-inbox",
                str(tmp_path / "handoffs"),
            ]
        )
        == 0
    )
    assert seen == [
        ("start", {"target": tmp_path, "title": "Build Loop", "force": True}),
        (
            "end",
            {
                "target": tmp_path,
                "note": "done",
                "handoff": True,
                "handoff_inbox": tmp_path / "handoffs",
            },
        ),
    ]


def test_work_note_cli(tmp_path, monkeypatch):
    seen = {}

    def fake_note(**kwargs):
        seen.update(kwargs)
        return 0

    monkeypatch.setattr(work_cmd, "note", fake_note)

    assert cli.main(["work", "note", "wired", "tests", "--target", str(tmp_path)]) == 0
    assert seen == {"target": tmp_path, "text": "wired tests"}


def test_work_run_cli(tmp_path, monkeypatch):
    seen = {}

    def fake_run(task, **kwargs):
        seen["task"] = task
        seen.update(kwargs)
        return 0

    monkeypatch.setattr(work_cmd, "run", fake_run)

    assert (
        cli.main(
            [
                "work",
                "run",
                "review",
                "repo",
                "--target",
                str(tmp_path),
                "--title",
                "Daily",
                "--output-dir",
                str(tmp_path / "run"),
                "--handoff-inbox",
                str(tmp_path / "handoffs"),
                "--no-handoff",
                "--dogfood-handoff",
                "--no-inspect",
                "--native-read-only-sandbox",
                "--timeout-seconds",
                "12",
                "--recap-limit",
                "2",
            ]
        )
        == 0
    )
    assert seen == {
        "task": "review repo",
        "target": tmp_path,
        "title": "Daily",
        "output_dir": tmp_path / "run",
        "handoff": False,
        "handoff_inbox": tmp_path / "handoffs",
        "dogfood_handoff": True,
        "inspect": False,
        "native_read_only_sandbox": True,
        "timeout_seconds": 12.0,
        "recap_limit": 2,
    }


def test_work_inspection_cli(tmp_path, monkeypatch):
    seen = []

    def fake_list(**kwargs):
        seen.append(("list", kwargs))
        return 0

    def fake_latest(**kwargs):
        seen.append(("latest", kwargs))
        return 0

    def fake_show(**kwargs):
        seen.append(("show", kwargs))
        return 0

    def fake_recap(**kwargs):
        seen.append(("recap", kwargs))
        return 0

    monkeypatch.setattr(work_cmd, "list_sessions", fake_list)
    monkeypatch.setattr(work_cmd, "latest", fake_latest)
    monkeypatch.setattr(work_cmd, "show", fake_show)
    monkeypatch.setattr(work_cmd, "recap", fake_recap)

    assert cli.main(["work", "list", "--target", str(tmp_path), "--limit", "2"]) == 0
    assert cli.main(["work", "latest", "--target", str(tmp_path)]) == 0
    assert cli.main(["work", "show", "abc123", "--target", str(tmp_path)]) == 0
    assert cli.main(["work", "recap", "--target", str(tmp_path), "--since", "2026-05-26", "--limit", "3"]) == 0
    assert seen == [
        ("list", {"target": tmp_path, "limit": 2}),
        ("latest", {"target": tmp_path}),
        ("show", {"target": tmp_path, "session": "abc123"}),
        ("recap", {"target": tmp_path, "limit": 3, "since": "2026-05-26"}),
    ]
