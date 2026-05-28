import json
import os
import socket
import subprocess
import sys
from datetime import date, datetime, timezone
from pathlib import Path

from brigade import cli
from brigade import dogfood_cmd
from brigade import security_cmd
from brigade import tools_cmd
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
    security_cmd.init(target=tmp_path)
    security_dir = tmp_path / ".brigade" / "security" / "latest"
    security_dir.mkdir(parents=True)
    _write_json(
        security_dir / "security-report.json",
        {"generated_at": "2026-05-26T12:00:00Z", "finding_count": 0, "policy": "personal"},
    )
    (security_dir / "security-report.md").write_text("# Brigade Security Report\n")
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
    assert "[ok] security_config:" in out
    assert "[ok] security_evidence:" in out
    assert "[ok] codex: /usr/bin/codex" in out
    assert "[ok] latest_next: Build doctor." in out
    assert "[ok] ready: daily work loop is usable" in out


def test_work_doctor_warns_for_task_acceptance_gh_and_stale_session(tmp_path, monkeypatch, capsys):
    _init_git_repo(tmp_path)
    dogfood_cmd.init(target=tmp_path)
    monkeypatch.setattr(work_cmd.shutil, "which", lambda name: "/usr/bin/codex" if name == "codex" else None)
    monkeypatch.setattr(dogfood_cmd, "_check_git_ignored", lambda repo, path: "yes")
    monkeypatch.setattr(
        work_cmd,
        "_now",
        lambda: datetime(2026, 5, 25, 8, 0, 0, tzinfo=timezone.utc),
    )
    assert work_cmd.task_add(target=tmp_path, text="Task without acceptance") == 0
    capsys.readouterr()
    ledger = json.loads((tmp_path / ".brigade" / "work" / "tasks.json").read_text())
    ledger["tasks"].append(
        {
            "id": "issue-task",
            "text": "Issue task",
            "status": "pending",
            "source": "github_issue",
            "type": "bug",
            "priority": "normal",
            "created_at": "2026-05-25T08:00:00+00:00",
            "updated_at": "2026-05-25T08:00:00+00:00",
            "acceptance": ["Issue task acceptance."],
            "metadata": {
                "github_issue": {
                    "url": "https://github.com/acme/widgets/issues/9",
                    "number": 9,
                    "title": "Issue task",
                    "labels": ["bug"],
                    "state": "OPEN",
                    "source": "gh",
                }
            },
        }
    )
    _write_json(tmp_path / ".brigade" / "work" / "tasks.json", ledger)
    assert work_cmd.start(target=tmp_path, title="Old active session") == 0
    capsys.readouterr()
    monkeypatch.setattr(
        work_cmd,
        "_now",
        lambda: datetime(2026, 5, 27, 10, 0, 0, tzinfo=timezone.utc),
    )

    assert work_cmd.doctor(target=tmp_path) == 0
    out = capsys.readouterr().out
    assert "[warn] active_session_age:" in out
    assert "[warn] task_acceptance: 1 pending task(s) missing acceptance criteria" in out
    assert "[warn] github_issues: 1 issue-backed task(s) cannot be checked because gh is missing: issue-task" in out


def test_work_doctor_warns_when_issue_backed_task_is_closed(tmp_path, monkeypatch, capsys):
    _init_git_repo(tmp_path)
    dogfood_cmd.init(target=tmp_path)
    monkeypatch.setattr(
        work_cmd.shutil,
        "which",
        lambda name: f"/usr/bin/{name}" if name in {"codex", "gh"} else None,
    )
    monkeypatch.setattr(dogfood_cmd, "_check_git_ignored", lambda repo, path: "yes")
    (tmp_path / ".brigade" / "work").mkdir(parents=True)
    _write_json(
        tmp_path / ".brigade" / "work" / "tasks.json",
        {
            "version": 1,
            "tasks": [
                {
                    "id": "issue-task",
                    "text": "Issue task",
                    "status": "pending",
                    "source": "github_issue",
                    "type": "bug",
                    "priority": "normal",
                    "created_at": "2026-05-25T08:00:00+00:00",
                    "updated_at": "2026-05-25T08:00:00+00:00",
                    "acceptance": ["Issue task acceptance."],
                    "metadata": {
                        "github_issue": {
                            "url": "https://github.com/acme/widgets/issues/9",
                            "number": 9,
                            "title": "Issue task",
                            "labels": ["bug"],
                            "state": "OPEN",
                            "source": "gh",
                        }
                    },
                }
            ],
        },
    )

    def fake_run(args, **kwargs):
        return subprocess.CompletedProcess(
            args,
            0,
            stdout=json.dumps(
                {
                    "url": "https://github.com/acme/widgets/issues/9",
                    "number": 9,
                    "title": "Issue task",
                    "labels": [{"name": "bug"}],
                    "state": "CLOSED",
                }
            ),
            stderr="",
        )

    monkeypatch.setattr(work_cmd.subprocess, "run", fake_run)

    assert work_cmd.doctor(target=tmp_path) == 0
    out = capsys.readouterr().out
    assert "[warn] github_issues_closed: 1 remote issue(s) are closed: issue-task" in out


def test_work_doctor_warns_for_scanner_queue_health(tmp_path, monkeypatch, capsys):
    _init_git_repo(tmp_path)
    dogfood_cmd.init(target=tmp_path)
    monkeypatch.setattr(work_cmd.shutil, "which", lambda name: "/usr/bin/codex" if name == "codex" else None)
    monkeypatch.setattr(dogfood_cmd, "_check_git_ignored", lambda repo, path: "yes")
    monkeypatch.setattr(
        work_cmd,
        "_now",
        lambda: datetime(2026, 5, 30, 12, 0, 0, tzinfo=timezone.utc),
    )
    imports = [
        {
            "id": "stale-task",
            "kind": "task",
            "source": "repo-scan",
            "text": "Stale task import",
            "status": "pending",
            "created_at": "2026-05-25T12:00:00+00:00",
            "updated_at": "2026-05-25T12:00:00+00:00",
        }
    ]
    for index in range(work_cmd.DISMISSED_SOURCE_WARN_THRESHOLD):
        imports.append(
            {
                "id": f"dismissed-{index}",
                "kind": "task",
                "source": "noisy-scan",
                "text": f"Noisy import {index}",
                "status": "dismissed",
                "created_at": "2026-05-29T12:00:00+00:00",
                "updated_at": "2026-05-29T12:00:00+00:00",
            }
        )
    work_cmd._write_imports(tmp_path, imports)

    assert work_cmd.doctor(target=tmp_path) == 0
    out = capsys.readouterr().out
    assert "[warn] scanner_imports_stale: 1 pending import(s) older than 72h: stale-task" in out
    assert "[warn] scanner_import_acceptance: 1 pending task import(s) missing acceptance criteria: stale-task" in out
    assert "[warn] scanner_import_noise: dismissed import threshold 5: noisy-scan=5" in out


def test_work_doctor_fails_invalid_security_config(tmp_path, monkeypatch, capsys):
    _init_git_repo(tmp_path)
    dogfood_cmd.init(target=tmp_path)
    security_config = tmp_path / ".brigade" / "security.toml"
    security_config.write_text('policy = "not-real"\n')
    monkeypatch.setattr(work_cmd.shutil, "which", lambda name: f"/usr/bin/{name}" if name == "codex" else None)
    monkeypatch.setattr(dogfood_cmd, "_check_git_ignored", lambda repo, path: "yes")

    assert work_cmd.doctor(target=tmp_path) == 1
    out = capsys.readouterr().out
    assert "[fail] security_config:" in out
    assert "invalid" in out
    assert "[fail] ready: 1 blocker" in out


def test_work_doctor_warns_on_stale_security_suppressions(tmp_path, monkeypatch, capsys):
    _init_git_repo(tmp_path)
    dogfood_cmd.init(target=tmp_path)
    security_config = tmp_path / ".brigade" / "security.toml"
    security_config.write_text(
        "\n".join(
            [
                'policy = "personal"',
                'fail_on = "critical"',
                "include_templates = false",
                "",
                "[suppressions]",
                'fingerprints = ["0123456789abcdef"]',
                "",
                "[suppression_reasons]",
                "",
            ]
        )
    )
    monkeypatch.setattr(work_cmd.shutil, "which", lambda name: f"/usr/bin/{name}" if name == "codex" else None)
    monkeypatch.setattr(dogfood_cmd, "_check_git_ignored", lambda repo, path: "yes")

    assert work_cmd.doctor(target=tmp_path) == 0
    out = capsys.readouterr().out
    assert "[warn] security_stale_suppressions:" in out
    assert "[warn] security_suppression_reasons:" in out


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


def test_work_brief_reports_morning_entrypoint(tmp_path, monkeypatch, capsys):
    _init_git_repo(tmp_path)
    dogfood_cmd.init(target=tmp_path)
    run_dir = tmp_path / ".brigade" / "runs" / "latest"
    run_dir.mkdir(parents=True)
    _write_json(run_dir / "run.json", {"started_at": "2026-05-26T12:10:00Z", "status": "ok", "task": "review"})
    (run_dir / "final.txt").write_text("Done.\n")
    (run_dir / "summary.md").write_text("# Summary\n\n## Next\n\nBuild the morning brief.\n\n## Final\n")
    times = iter(
        [
            datetime(2026, 5, 26, 12, 0, 0, tzinfo=timezone.utc),
            datetime(2026, 5, 26, 13, 0, 0, tzinfo=timezone.utc),
        ]
    )
    monkeypatch.setattr(work_cmd, "_now", lambda: next(times))
    monkeypatch.setattr(work_cmd.shutil, "which", lambda name: f"/usr/bin/{name}")
    assert work_cmd.start(target=tmp_path, title="Ended Work") == 0
    assert work_cmd.end(target=tmp_path, note="done", handoff=True, handoff_inbox=tmp_path / "handoffs") == 0

    assert work_cmd.brief(target=tmp_path, limit=2) == 0
    out = capsys.readouterr().out
    assert "work brief:" in out
    assert "active_session: none" in out
    assert "latest_session:" in out
    assert "latest_session_title: Ended Work" in out
    assert "dogfood_ready: True" in out
    assert "latest_run: 2026-05-26T12:10:00Z [ok]" in out
    assert "next_source: latest_dogfood_run" in out
    assert "next: Build the morning brief." in out
    assert "suggested_command: brigade work run 'Build the morning brief.'" in out
    assert "recent_sessions:" in out


def test_work_brief_json_reports_recent_sessions(tmp_path, monkeypatch, capsys):
    _init_git_repo(tmp_path)
    dogfood_cmd.init(target=tmp_path)
    run_dir = tmp_path / ".brigade" / "runs" / "latest"
    run_dir.mkdir(parents=True)
    _write_json(run_dir / "run.json", {"started_at": "2026-05-26T12:10:00Z", "status": "ok", "task": "review"})
    (run_dir / "final.txt").write_text("Done.\n\n## Next\n\nBuild JSON brief.\n")
    monkeypatch.setattr(
        work_cmd,
        "_now",
        lambda: datetime(2026, 5, 26, 12, 0, 0, tzinfo=timezone.utc),
    )
    monkeypatch.setattr(work_cmd.shutil, "which", lambda name: f"/usr/bin/{name}")
    assert work_cmd.start(target=tmp_path, title="Active Work") == 0
    capsys.readouterr()

    assert work_cmd.brief(target=tmp_path, json_output=True) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["active_session"]["title"] == "Active Work"
    assert payload["latest_session"]["title"] == "Active Work"
    assert payload["recent_sessions"][0]["status"] == "active"
    assert payload["dogfood"]["next_source"] == "final"
    assert payload["next"] == "Build JSON brief."
    assert payload["suggested_command"] == 'brigade work end --note "..." --handoff'


def test_work_task_ledger_add_list_show_and_done(tmp_path, monkeypatch, capsys):
    _init_git_repo(tmp_path)
    times = iter(
        [
            datetime(2026, 5, 26, 12, 0, 0, tzinfo=timezone.utc),
            datetime(2026, 5, 26, 12, 30, 0, tzinfo=timezone.utc),
        ]
    )
    monkeypatch.setattr(work_cmd, "_now", lambda: next(times))

    assert work_cmd.task_add(target=tmp_path, text="Build task ledger") == 0
    out = capsys.readouterr().out
    assert "task:" in out
    task_id = out.split("task: ", 1)[1].splitlines()[0]

    assert work_cmd.tasks(target=tmp_path) == 0
    out = capsys.readouterr().out
    assert "work tasks:" in out
    assert task_id in out
    assert "[pending] [task normal acceptance=0] Build task ledger" in out

    assert work_cmd.task_show(target=tmp_path, task_id=task_id[:12]) == 0
    out = capsys.readouterr().out
    assert f"task: {task_id}" in out
    assert "status: pending" in out
    assert "type: task" in out
    assert "priority: normal" in out
    assert "acceptance: 0" in out
    assert "text: Build task ledger" in out

    assert work_cmd.task_done(target=tmp_path, task_id=task_id[:12]) == 0
    assert "status: done" in capsys.readouterr().out
    assert work_cmd.tasks(target=tmp_path) == 0
    assert "tasks: none" in capsys.readouterr().out
    assert work_cmd.tasks(target=tmp_path, all_tasks=True, json_output=True) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["tasks"][0]["status"] == "done"
    assert payload["tasks"][0]["completed_at"] == "2026-05-26T12:30:00+00:00"


def test_work_task_add_from_next_deduplicates_pending_task(tmp_path, monkeypatch, capsys):
    _init_git_repo(tmp_path)
    dogfood_cmd.init(target=tmp_path)
    run_dir = tmp_path / ".brigade" / "runs" / "latest"
    run_dir.mkdir(parents=True)
    _write_json(run_dir / "run.json", {"started_at": "2026-05-26T12:10:00Z", "status": "ok", "task": "review"})
    (run_dir / "final.txt").write_text("Done.\n\nNext step: Build from extracted next.\n")
    monkeypatch.setattr(
        work_cmd,
        "_now",
        lambda: datetime(2026, 5, 26, 12, 0, 0, tzinfo=timezone.utc),
    )

    assert work_cmd.task_add(target=tmp_path, from_next=True) == 0
    out = capsys.readouterr().out
    assert "Build from extracted next." in out
    assert "created: True" in out
    first_id = out.split("task: ", 1)[1].splitlines()[0]
    assert work_cmd.task_add(target=tmp_path, from_next=True) == 0
    out = capsys.readouterr().out
    assert f"task: {first_id}" in out
    assert "created: False" in out
    assert work_cmd.next(target=tmp_path, json_output=True) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["next_source"] == "task_ledger"
    assert payload["next"] == "Build from extracted next."
    assert payload["task_id"]


def test_work_task_add_stores_metadata_acceptance_and_plan(tmp_path, monkeypatch, capsys):
    _init_git_repo(tmp_path)
    monkeypatch.setattr(
        work_cmd,
        "_now",
        lambda: datetime(2026, 5, 26, 12, 0, 0, tzinfo=timezone.utc),
    )

    assert (
        work_cmd.task_add(
            target=tmp_path,
            text="Build issue loop",
            task_type="feature",
            priority="high",
            acceptance=["Adds metadata", "Shows criteria in the plan"],
        )
        == 0
    )
    out = capsys.readouterr().out
    task_id = out.split("task: ", 1)[1].splitlines()[0]
    assert "type: feature" in out
    assert "priority: high" in out
    assert "acceptance: 2" in out

    assert work_cmd.task_plan(target=tmp_path, task_id=task_id[:12]) == 0
    out = capsys.readouterr().out
    assert "task: " in out
    assert "type: feature" in out
    assert "priority: high" in out
    assert "  - Adds metadata" in out
    assert "  - Shows criteria in the plan" in out
    assert "suggested_command: brigade work run" in out

    assert work_cmd.task_plan(target=tmp_path, task_id=task_id[:12], json_output=True) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["type"] == "feature"
    assert payload["priority"] == "high"
    assert payload["acceptance_count"] == 2
    assert payload["acceptance_missing"] is False


def test_work_task_add_template_preserves_explicit_acceptance_and_plan(tmp_path, monkeypatch, capsys):
    _init_git_repo(tmp_path)
    monkeypatch.setattr(
        work_cmd,
        "_now",
        lambda: datetime(2026, 5, 26, 12, 0, 0, tzinfo=timezone.utc),
    )

    assert (
        work_cmd.task_add(
            target=tmp_path,
            text="Fix login redirect",
            task_type="bug",
            priority="high",
            template="bugfix",
            acceptance=["The login redirect works in the browser smoke test."],
        )
        == 0
    )
    out = capsys.readouterr().out
    assert "template: bugfix" in out
    task_id = out.split("task: ", 1)[1].splitlines()[0]
    ledger = json.loads((tmp_path / ".brigade" / "work" / "tasks.json").read_text())
    task = ledger["tasks"][0]
    assert task["type"] == "bug"
    assert task["priority"] == "high"
    assert task["template"] == "bugfix"
    assert "The bug is reproduced by a focused failing test or equivalent fixture." in task["acceptance"]
    assert "The login redirect works in the browser smoke test." in task["acceptance"]

    assert work_cmd.task_plan(target=tmp_path, task_id=task_id[:12]) == 0
    out = capsys.readouterr().out
    assert "template: bugfix" in out
    assert "guidance:" in out
    assert "Reproduce the failing behavior first." in out
    assert "The login redirect works in the browser smoke test." in out


def test_extract_issue_acceptance_from_sections_and_checkboxes():
    body = """
## Context
- This is background, not acceptance.

## Acceptance Criteria
- CLI imports the first criterion.
1. Numbered criteria are supported.

## Notes
- This should not be imported.
- [ ] Checkboxes are imported wherever they appear.

Testing:
* Focused tests pass.
"""

    assert work_cmd._extract_issue_acceptance(body) == [
        "CLI imports the first criterion.",
        "Numbered criteria are supported.",
        "Checkboxes are imported wherever they appear.",
        "Focused tests pass.",
    ]


def test_extract_issue_acceptance_returns_empty_for_missing_body():
    assert work_cmd._extract_issue_acceptance(None) == []
    assert work_cmd._extract_issue_acceptance("") == []


def test_work_task_add_from_issue_preserves_github_metadata(tmp_path, monkeypatch, capsys):
    _init_git_repo(tmp_path)
    monkeypatch.setattr(
        work_cmd,
        "_now",
        lambda: datetime(2026, 5, 26, 12, 0, 0, tzinfo=timezone.utc),
    )
    monkeypatch.setattr(work_cmd.shutil, "which", lambda name: "/usr/bin/gh" if name == "gh" else None)

    def fake_run(args, **kwargs):
        assert args[:3] == ["gh", "issue", "view"]
        return subprocess.CompletedProcess(
            args,
            0,
            stdout=json.dumps(
                {
                    "url": "https://github.com/acme/widgets/issues/42",
                    "number": 42,
                    "title": "Import issue backed task",
                    "labels": [{"name": "bug"}, {"name": "tdd"}],
                    "state": "OPEN",
                }
            ),
            stderr="",
        )

    monkeypatch.setattr(work_cmd.subprocess, "run", fake_run)

    assert work_cmd.task_add(target=tmp_path, from_issue="42", template="red-green-refactor") == 0
    out = capsys.readouterr().out
    assert "issue: https://github.com/acme/widgets/issues/42" in out
    ledger = json.loads((tmp_path / ".brigade" / "work" / "tasks.json").read_text())
    task = ledger["tasks"][0]
    assert task["text"] == "Import issue backed task"
    assert task["source"] == "github_issue"
    assert task["metadata"]["github_issue"] == {
        "url": "https://github.com/acme/widgets/issues/42",
        "number": 42,
        "title": "Import issue backed task",
        "labels": ["bug", "tdd"],
        "state": "OPEN",
        "source": "gh",
        "ref": "42",
    }


def test_work_task_add_from_issue_imports_body_acceptance(tmp_path, monkeypatch, capsys):
    _init_git_repo(tmp_path)
    monkeypatch.setattr(
        work_cmd,
        "_now",
        lambda: datetime(2026, 5, 26, 12, 0, 0, tzinfo=timezone.utc),
    )
    monkeypatch.setattr(work_cmd.shutil, "which", lambda name: "/usr/bin/gh" if name == "gh" else None)

    def fake_run(args, **kwargs):
        assert args[:3] == ["gh", "issue", "view"]
        assert args[-1] == "url,number,title,labels,state,body"
        return subprocess.CompletedProcess(
            args,
            0,
            stdout=json.dumps(
                {
                    "url": "https://github.com/acme/widgets/issues/43",
                    "number": 43,
                    "title": "Extract issue acceptance",
                    "labels": [],
                    "state": "OPEN",
                    "body": """
## Acceptance Criteria
- Parse acceptance section bullets.
- Keep the existing ledger acceptance path.

## Notes
- Ignore unrelated bullets.
""",
                }
            ),
            stderr="",
        )

    monkeypatch.setattr(work_cmd.subprocess, "run", fake_run)

    assert work_cmd.task_add(target=tmp_path, from_issue="43", acceptance=["Manual criterion"]) == 0
    out = capsys.readouterr().out
    assert "acceptance: 3" in out
    ledger = json.loads((tmp_path / ".brigade" / "work" / "tasks.json").read_text())
    task = ledger["tasks"][0]
    assert task["acceptance"] == [
        "Parse acceptance section bullets.",
        "Keep the existing ledger acceptance path.",
        "Manual criterion",
    ]
    assert "body" not in task["metadata"]["github_issue"]
    assert "acceptance" not in task["metadata"]["github_issue"]


def test_work_run_uses_issue_imported_acceptance(tmp_path, monkeypatch):
    _init_git_repo(tmp_path)
    artifacts_dir = tmp_path / ".brigade" / "runs"
    dogfood_cmd.init(target=tmp_path, artifacts_dir=artifacts_dir)
    times = iter(
        [
            datetime(2026, 5, 26, 11, 30, 0, tzinfo=timezone.utc),
            datetime(2026, 5, 26, 12, 0, 0, tzinfo=timezone.utc),
            datetime(2026, 5, 26, 13, 0, 0, tzinfo=timezone.utc),
            datetime(2026, 5, 26, 13, 0, 1, tzinfo=timezone.utc),
        ]
    )
    monkeypatch.setattr(work_cmd, "_now", lambda: next(times))
    monkeypatch.setattr(work_cmd.shutil, "which", lambda name: "/usr/bin/gh" if name == "gh" else None)

    def fake_gh_run(args, **kwargs):
        return subprocess.CompletedProcess(
            args,
            0,
            stdout=json.dumps(
                {
                    "url": "https://github.com/acme/widgets/issues/44",
                    "number": 44,
                    "title": "Run issue accepted task",
                    "labels": [],
                    "state": "OPEN",
                    "body": "Acceptance Criteria:\n- Imported issue criterion reaches dogfood.",
                }
            ),
            stderr="",
        )

    monkeypatch.setattr(work_cmd.subprocess, "run", fake_gh_run)
    assert work_cmd.task_add(target=tmp_path, from_issue="44") == 0
    seen = {}

    def fake_dogfood_run(task, **kwargs):
        seen["task"] = task
        run_dir = kwargs["output_dir"]
        run_dir.mkdir(parents=True)
        _write_json(run_dir / "run.json", {"started_at": "2026-05-26T12:10:00Z", "status": "ok", "task": task})
        (run_dir / "final.txt").write_text("Done.\n\nNext step: Build follow-up.\n")
        return 0

    monkeypatch.setattr(dogfood_cmd, "run", fake_dogfood_run)

    assert work_cmd.run(None, target=tmp_path, output_dir=artifacts_dir / "new", handoff=False) == 0
    assert seen["task"].startswith("Run issue accepted task")
    assert "- Imported issue criterion reaches dogfood." in seen["task"]


def test_work_task_add_from_issue_fails_without_partial_task(tmp_path, monkeypatch, capsys):
    _init_git_repo(tmp_path)
    monkeypatch.setattr(work_cmd.shutil, "which", lambda name: None)

    assert work_cmd.task_add(target=tmp_path, from_issue="42") == 1
    assert "gh CLI is not available" in capsys.readouterr().err
    assert not (tmp_path / ".brigade" / "work" / "tasks.json").exists()


def test_work_task_add_from_issue_rejects_malformed_gh_output_without_partial_task(tmp_path, monkeypatch, capsys):
    _init_git_repo(tmp_path)
    monkeypatch.setattr(work_cmd.shutil, "which", lambda name: "/usr/bin/gh" if name == "gh" else None)

    def fake_run(args, **kwargs):
        return subprocess.CompletedProcess(args, 0, stdout="{bad json", stderr="")

    monkeypatch.setattr(work_cmd.subprocess, "run", fake_run)

    assert work_cmd.task_add(target=tmp_path, from_issue="42") == 1
    assert "returned invalid JSON" in capsys.readouterr().err
    assert not (tmp_path / ".brigade" / "work" / "tasks.json").exists()


def test_work_brief_includes_pending_tasks(tmp_path, monkeypatch, capsys):
    _init_git_repo(tmp_path)
    monkeypatch.setattr(
        work_cmd,
        "_now",
        lambda: datetime(2026, 5, 26, 12, 0, 0, tzinfo=timezone.utc),
    )
    assert work_cmd.task_add(target=tmp_path, text="Build queued task") == 0
    capsys.readouterr()

    assert work_cmd.brief(target=tmp_path, json_output=True) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["next_source"] == "task_ledger"
    assert payload["next"] == "Build queued task"
    assert payload["pending_tasks"][0]["text"] == "Build queued task"
    assert payload["suggested_command"] == "brigade work run"
    assert payload["next_task"]["acceptance_missing"] is True
    assert payload["next_task"]["acceptance_count"] == 0


def test_work_brief_reports_next_task_acceptance(tmp_path, monkeypatch, capsys):
    _init_git_repo(tmp_path)
    monkeypatch.setattr(
        work_cmd,
        "_now",
        lambda: datetime(2026, 5, 26, 12, 0, 0, tzinfo=timezone.utc),
    )
    assert (
        work_cmd.task_add(
            target=tmp_path,
            text="Build accepted task",
            task_type="workflow",
            priority="urgent",
            acceptance=["Brief reports acceptance"],
        )
        == 0
    )
    capsys.readouterr()

    assert work_cmd.brief(target=tmp_path) == 0
    out = capsys.readouterr().out
    assert "next_type: workflow" in out
    assert "next_priority: urgent" in out
    assert "next_acceptance: 1" in out
    assert "[workflow urgent acceptance=1] Build accepted task" in out


def test_work_brief_surfaces_issue_backed_next_task_context(tmp_path, monkeypatch, capsys):
    _init_git_repo(tmp_path)
    monkeypatch.setattr(
        work_cmd,
        "_now",
        lambda: datetime(2026, 5, 26, 12, 0, 0, tzinfo=timezone.utc),
    )
    monkeypatch.setattr(work_cmd.shutil, "which", lambda name: "/usr/bin/gh" if name == "gh" else None)

    def fake_run(args, **kwargs):
        return subprocess.CompletedProcess(
            args,
            0,
            stdout=json.dumps(
                {
                    "url": "https://github.com/acme/widgets/issues/7",
                    "number": 7,
                    "title": "Surface issue context",
                    "labels": [{"name": "docs"}],
                    "state": "OPEN",
                }
            ),
            stderr="",
        )

    monkeypatch.setattr(work_cmd.subprocess, "run", fake_run)
    assert work_cmd.task_add(target=tmp_path, from_issue="7") == 0
    capsys.readouterr()

    assert work_cmd.brief(target=tmp_path, json_output=True) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["next_issue"]["url"] == "https://github.com/acme/widgets/issues/7"
    assert payload["next_issue"]["labels"] == ["docs"]

    assert work_cmd.brief(target=tmp_path) == 0
    out = capsys.readouterr().out
    assert "issue: https://github.com/acme/widgets/issues/7" in out
    assert "issue_state: OPEN" in out
    assert "issue_labels: docs" in out


def test_work_import_add_list_show_and_promote(tmp_path, monkeypatch, capsys):
    _init_git_repo(tmp_path)
    times = iter(
        [
            datetime(2026, 5, 26, 12, 0, 0, tzinfo=timezone.utc),
            datetime(2026, 5, 26, 12, 30, 0, tzinfo=timezone.utc),
            datetime(2026, 5, 26, 12, 30, 1, tzinfo=timezone.utc),
        ]
    )
    monkeypatch.setattr(work_cmd, "_now", lambda: next(times))

    assert (
        work_cmd.import_add(
            target=tmp_path,
            text="Refresh the stale memory card",
            kind="task",
            source="slack",
            metadata=["channel=eng", "thread=abc123"],
        )
        == 0
    )
    out = capsys.readouterr().out
    assert "import:" in out
    assert "kind: task" in out
    assert "source: slack" in out
    import_id = out.split("import: ", 1)[1].splitlines()[0]

    assert work_cmd.import_list(target=tmp_path) == 0
    out = capsys.readouterr().out
    assert "work imports:" in out
    assert import_id in out
    assert "[pending] task from slack: Refresh the stale memory card" in out

    assert work_cmd.import_show(target=tmp_path, import_id=import_id[:12]) == 0
    out = capsys.readouterr().out
    assert f"import: {import_id}" in out
    assert "status: pending" in out
    assert "channel: eng" in out
    assert "thread: abc123" in out

    assert work_cmd.import_promote(target=tmp_path, import_id=import_id[:12]) == 0
    out = capsys.readouterr().out
    assert "status: promoted" in out
    assert "created: True" in out
    task_id = out.split("task: ", 1)[1].splitlines()[0]

    assert work_cmd.tasks(target=tmp_path, json_output=True) == 0
    payload = json.loads(capsys.readouterr().out)
    task = payload["tasks"][0]
    assert task["id"] == task_id
    assert task["text"] == "Refresh the stale memory card"
    assert task["source"] == "import:slack"
    assert task["metadata"]["import_id"] == import_id
    assert task["metadata"]["import_kind"] == "task"
    assert task["metadata"]["import_source"] == "slack"
    assert task["metadata"]["channel"] == "eng"

    assert work_cmd.import_list(target=tmp_path) == 0
    assert "imports: none" in capsys.readouterr().out
    assert work_cmd.import_list(target=tmp_path, all_imports=True, json_output=True) == 0
    imports_payload = json.loads(capsys.readouterr().out)
    assert imports_payload["imports"][0]["status"] == "promoted"
    assert imports_payload["imports"][0]["task_id"] == task_id


def test_work_import_promote_reuses_existing_pending_task(tmp_path, monkeypatch, capsys):
    _init_git_repo(tmp_path)
    times = iter(
        [
            datetime(2026, 5, 26, 12, 0, 0, tzinfo=timezone.utc),
            datetime(2026, 5, 26, 12, 1, 0, tzinfo=timezone.utc),
            datetime(2026, 5, 26, 12, 2, 0, tzinfo=timezone.utc),
        ]
    )
    monkeypatch.setattr(work_cmd, "_now", lambda: next(times))
    assert work_cmd.task_add(target=tmp_path, text="Refresh stale card") == 0
    task_id = capsys.readouterr().out.split("task: ", 1)[1].splitlines()[0]
    assert work_cmd.import_add(target=tmp_path, text=" refresh  stale   card ", source="memory-care") == 0
    import_id = capsys.readouterr().out.split("import: ", 1)[1].splitlines()[0]

    assert work_cmd.import_promote(target=tmp_path, import_id=import_id) == 0
    out = capsys.readouterr().out
    assert f"task: {task_id}" in out
    assert "created: False" in out
    ledger = json.loads((tmp_path / ".brigade" / "work" / "tasks.json").read_text())
    assert len(ledger["tasks"]) == 1


def test_work_import_validate_and_ingest_jsonl(tmp_path, monkeypatch, capsys):
    _init_git_repo(tmp_path)
    monkeypatch.setattr(
        work_cmd,
        "_now",
        lambda: datetime(2026, 5, 26, 12, 0, 0, tzinfo=timezone.utc),
    )
    import_file = tmp_path / "imports.jsonl"
    import_file.write_text(
        json.dumps(
            {
                "text": "Review imported scanner item",
                "kind": "finding",
                "source": "scanner",
                "metadata": {"thread": "abc123"},
            }
        )
        + "\n"
    )

    assert work_cmd.import_validate(input_path=import_file) == 0
    out = capsys.readouterr().out
    assert "status: valid" in out
    assert "records: 1" in out

    assert work_cmd.import_ingest(target=tmp_path, input_path=import_file) == 0
    out = capsys.readouterr().out
    assert "imported: 1" in out
    assert "skipped_duplicates: 0" in out
    assert work_cmd.import_ingest(target=tmp_path, input_path=import_file) == 0
    out = capsys.readouterr().out
    assert "imported: 0" in out
    assert "skipped_duplicates: 1" in out

    assert work_cmd.import_list(target=tmp_path, json_output=True) == 0
    payload = json.loads(capsys.readouterr().out)
    assert len(payload["imports"]) == 1
    assert payload["imports"][0]["kind"] == "finding"
    assert payload["imports"][0]["source"] == "scanner"
    assert payload["imports"][0]["metadata"]["thread"] == "abc123"


def test_work_import_validate_ingest_and_promote_task_metadata(tmp_path, monkeypatch, capsys):
    _init_git_repo(tmp_path)
    times = iter(
        [
            datetime(2026, 5, 26, 12, 0, 0, tzinfo=timezone.utc),
            datetime(2026, 5, 26, 12, 1, 0, tzinfo=timezone.utc),
            datetime(2026, 5, 26, 12, 1, 1, tzinfo=timezone.utc),
        ]
    )
    monkeypatch.setattr(work_cmd, "_now", lambda: next(times))
    import_file = tmp_path / "task-imports.jsonl"
    import_file.write_text(
        json.dumps(
            {
                "text": "Build scanner task",
                "kind": "task",
                "source": "repo-scan",
                "type": "feature",
                "priority": "high",
                "template": "vertical-slice",
                "acceptance": ["Scanner acceptance passes."],
                "metadata": {"scanner": "daily"},
            }
        )
        + "\n"
    )

    assert work_cmd.import_validate(input_path=import_file) == 0
    assert "status: valid" in capsys.readouterr().out
    assert work_cmd.import_ingest(target=tmp_path, input_path=import_file) == 0
    assert "imported: 1" in capsys.readouterr().out
    assert work_cmd.import_list(target=tmp_path, json_output=True) == 0
    imports = json.loads(capsys.readouterr().out)["imports"]
    item = imports[0]
    assert item["type"] == "feature"
    assert item["priority"] == "high"
    assert item["template"] == "vertical-slice"
    assert item["acceptance"] == ["Scanner acceptance passes."]

    assert work_cmd.import_promote(target=tmp_path, import_id=item["id"]) == 0
    out = capsys.readouterr().out
    assert "acceptance: 4" in out
    task_id = out.split("task: ", 1)[1].splitlines()[0]
    ledger = json.loads((tmp_path / ".brigade" / "work" / "tasks.json").read_text())
    task = ledger["tasks"][0]
    assert task["id"] == task_id
    assert task["type"] == "feature"
    assert task["priority"] == "high"
    assert task["template"] == "vertical-slice"
    assert task["acceptance"] == [
        "One user-visible path is implemented end to end.",
        "Focused tests cover the new path.",
        "Documentation or help text is updated when user behavior changes.",
        "Scanner acceptance passes.",
    ]
    assert task["metadata"]["import_source"] == "repo-scan"
    assert task["metadata"]["scanner"] == "daily"


def test_work_inbox_groups_scanner_imports_and_reports_candidate(tmp_path, monkeypatch, capsys):
    _init_git_repo(tmp_path)
    monkeypatch.setattr(
        work_cmd,
        "_now",
        lambda: datetime(2026, 5, 29, 12, 0, 0, tzinfo=timezone.utc),
    )
    work_cmd._write_imports(
        tmp_path,
        [
            {
                "id": "old-task",
                "kind": "task",
                "source": "repo-scan",
                "text": "Old scanner task",
                "status": "pending",
                "priority": "low",
                "acceptance": [],
                "created_at": "2026-05-25T12:00:00+00:00",
                "updated_at": "2026-05-25T12:00:00+00:00",
            },
            {
                "id": "ready-task",
                "kind": "task",
                "source": "repo-scan",
                "text": "Ready scanner task",
                "status": "pending",
                "priority": "high",
                "acceptance": ["Ready acceptance."],
                "created_at": "2026-05-26T12:00:00+00:00",
                "updated_at": "2026-05-26T12:00:00+00:00",
            },
            {
                "id": "finding-one",
                "kind": "finding",
                "source": "security-scan",
                "text": "Review scanner finding",
                "status": "pending",
                "created_at": "2026-05-27T12:00:00+00:00",
                "updated_at": "2026-05-27T12:00:00+00:00",
            },
        ],
    )

    assert work_cmd.inbox(target=tmp_path) == 0
    out = capsys.readouterr().out
    assert "work inbox:" in out
    assert "pending_imports: 3" in out
    assert "repo-scan: 2" in out
    assert "task_acceptance_ready: 1" in out
    assert "task_acceptance_missing: 1" in out
    assert "import: ready-task" in out
    assert "run: brigade work import promote --run ready-task" in out

    assert work_cmd.inbox(target=tmp_path, json_output=True) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["counts"]["total"] == 3
    assert payload["counts"]["by_source"] == {"repo-scan": 2, "security-scan": 1}
    assert payload["counts"]["by_kind"] == {"finding": 1, "task": 2}
    assert payload["counts"]["by_priority"] == {"high": 1, "low": 1}
    assert payload["counts"]["acceptance"] == {"missing": 1, "ready": 1}
    assert payload["counts"]["stale"] == 1
    assert payload["candidate"]["id"] == "ready-task"


def test_work_scanners_init_list_show_plan_and_json(tmp_path, monkeypatch, capsys):
    _init_git_repo(tmp_path)
    monkeypatch.setattr(dogfood_cmd, "_check_git_ignored", lambda repo, path: "yes")

    assert work_cmd.scanners_init(target=tmp_path) == 0
    out = capsys.readouterr().out
    config = tmp_path / ".brigade" / "scanners.toml"
    assert f"scanner_config: {config}" in out
    assert "scanners: 7" in out
    assert ".brigade/scanners.toml" in (tmp_path / ".gitignore").read_text()

    assert work_cmd.scanners_list(target=tmp_path) == 0
    out = capsys.readouterr().out
    assert "work scanners:" in out
    assert "- chat-memory-sweep [enabled] daily@02:15 source=chat-memory-sweep" in out
    assert "brigade work import chat-sweep --json" in out

    assert work_cmd.scanners_list(target=tmp_path, json_output=True) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["valid"] is True
    assert payload["scanners"][0]["id"] == "chat-memory-sweep"

    assert work_cmd.scanners_show(target=tmp_path, scanner_id="memory-refresh") == 0
    out = capsys.readouterr().out
    assert "scanner: memory-refresh" in out
    assert "source: memory-refresh" in out

    assert work_cmd.scanners_plan(target=tmp_path) == 0
    out = capsys.readouterr().out
    assert "work scanners plan:" in out
    assert "planned:" in out
    assert "conflicts: none" in out
    assert "suggested_schedule:" in out

    assert work_cmd.scanners_plan(target=tmp_path, json_output=True) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["valid"] is True
    assert payload["planned"][0]["id"] == "handoff-ingest"
    assert payload["suggestions"]


def test_tools_init_list_show_search_doctor_and_json(tmp_path, monkeypatch, capsys):
    _init_git_repo(tmp_path)
    monkeypatch.setattr(dogfood_cmd, "_check_git_ignored", lambda repo, path: "yes")

    assert tools_cmd.init(target=tmp_path) == 0
    out = capsys.readouterr().out
    config = tmp_path / ".brigade" / "tools.toml"
    assert f"tools_config: {config}" in out
    assert "tools: 2" in out
    assert ".brigade/tools.toml" in (tmp_path / ".gitignore").read_text()

    (tmp_path / "tools").mkdir()
    (tmp_path / "tools" / "simplify.md").write_text("Simplify command\n")
    (tmp_path / "tools" / "superpowers.md").write_text("Superpowers\n")
    (tmp_path / ".claude" / "commands").mkdir(parents=True)
    (tmp_path / ".claude" / "commands" / "simplify.md").write_text("Simplify command\n")
    (tmp_path / ".claude" / "commands" / "superpowers.md").write_text("Superpowers\n")
    (tmp_path / ".codex" / "skills" / "simplify").mkdir(parents=True)
    (tmp_path / ".codex" / "skills" / "simplify" / "SKILL.md").write_text("Simplify skill\n")
    (tmp_path / ".codex" / "skills" / "superpowers").mkdir(parents=True)
    (tmp_path / ".codex" / "skills" / "superpowers" / "SKILL.md").write_text("Superpowers skill\n")
    (tmp_path / ".opencode" / "superpowers").mkdir(parents=True)
    (tmp_path / ".opencode" / "superpowers" / "superpowers.md").write_text("Superpowers projection\n")
    assert tools_cmd.apply(target=tmp_path, all_tools=True, force=True) == 0

    assert tools_cmd.list_tools(target=tmp_path) == 0
    out = capsys.readouterr().out
    assert "tools:" in out
    assert "- simplify [slash-command]" in out

    assert tools_cmd.list_tools(target=tmp_path, json_output=True) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["valid"] is True
    assert payload["tool_count"] == 2

    assert tools_cmd.show(target=tmp_path, tool_id="simplify") == 0
    out = capsys.readouterr().out
    assert "tool: simplify" in out
    assert "claude: current" in out
    assert "codex: current" in out

    assert tools_cmd.search(target=tmp_path, query="superpower", json_output=True) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["match_count"] == 1
    assert payload["matches"][0]["id"] == "superpowers"

    assert tools_cmd.doctor(target=tmp_path) == 0
    out = capsys.readouterr().out
    assert "[ok] tool_config:" in out
    assert "[ok] tool_catalog: no issues" in out


def test_tools_catalog_covers_portable_families_and_mcp_discovery(tmp_path, capsys):
    _init_git_repo(tmp_path)
    (tmp_path / ".brigade").mkdir()
    (tmp_path / "tools").mkdir()
    (tmp_path / "tools" / "skill.md").write_text("Skill source\n")
    (tmp_path / "tools" / "command.md").write_text("Slash command\n")
    (tmp_path / "tools" / "super.md").write_text("Superpower\n")
    (tmp_path / "tools" / "script.sh").write_text("#!/bin/sh\n")
    (tmp_path / "tools" / "mcp.json").write_text(
        json.dumps(
            {
                "mcpServers": {
                    "good": {"command": "brigade", "timeout": 10},
                    "bad": {},
                    "risky": {"command": "bash -c echo hi"},
                }
            }
        )
    )
    config = tmp_path / ".brigade" / "tools.toml"
    config.write_text(
        """
[[tool]]
id = "memory-skill"
name = "Memory Skill"
family = "skill"
enabled = true
description = "Portable memory skill."
source_path = "tools/skill.md"
supported_harnesses = []

[[tool]]
id = "simplify"
name = "Simplify"
family = "slash-command"
enabled = true
description = "Portable simplify command."
source_path = "tools/command.md"
supported_harnesses = []

[[tool]]
id = "superpowers"
name = "Superpowers"
family = "superpower"
enabled = true
description = "Portable superpower."
source_path = "tools/super.md"
supported_harnesses = []

[[tool]]
id = "script-tool"
name = "Script Tool"
family = "script"
enabled = true
description = "Portable script."
source_path = "tools/script.sh"
command = "brigade status"
supported_harnesses = []

[[tool]]
id = "mcp-local"
name = "MCP Local"
family = "mcp"
enabled = true
description = "Local MCP config."
manifest_path = "tools/mcp.json"
supported_harnesses = []
"""
    )

    assert tools_cmd.list_tools(target=tmp_path, json_output=True) == 0
    payload = json.loads(capsys.readouterr().out)
    assert {tool["family"] for tool in payload["tools"]} == {
        "skill",
        "slash-command",
        "superpower",
        "script",
        "mcp",
    }

    assert tools_cmd.show(target=tmp_path, tool_id="mcp-local", json_output=True) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["tool"]["mcp"]["server_count"] == 3
    assert payload["tool"]["mcp"]["server_ids"] == ["bad", "good", "risky"]

    assert tools_cmd.doctor(target=tmp_path) == 0
    out = capsys.readouterr().out
    assert "[warn] tool_missing_command: MCP server bad is missing command" in out
    assert "[warn] tool_missing_timeout: MCP server bad is missing timeout metadata" in out
    assert "[warn] tool_high_risk_command: MCP server risky command shape is high risk" in out


def test_tools_doctor_reports_parity_stale_schema_command_health_and_unsafe_fields(tmp_path, capsys):
    _init_git_repo(tmp_path)
    (tmp_path / ".brigade").mkdir()
    (tmp_path / "tools").mkdir()
    (tmp_path / "tools" / "source.md").write_text("Tool source\n")
    projection = tmp_path / ".claude" / "commands" / "tool.md"
    projection.parent.mkdir(parents=True)
    projection.write_text("projected\n")
    schema = tmp_path / "tools" / "schema.json"
    schema.write_text("{not json")
    health = tmp_path / "tools" / "health.json"
    health.write_text("{}\n")
    old = datetime(2026, 5, 25, 12, 0, 0, tzinfo=timezone.utc).timestamp()
    os.utime(health, (old, old))
    config = tmp_path / ".brigade" / "tools.toml"
    config.write_text(
        """
[[tool]]
id = "portable"
name = "Portable Tool"
family = "script"
enabled = true
description = "Portable script with several repairable issues."
source_path = "tools/source.md"
schema_path = "tools/schema.json"
health_path = "tools/health.json"
command = "missing-command --flag"
auth_label = "local"
password = "do-not-print"
supported_harnesses = ["claude", "codex"]
projections = { claude = ".claude/commands/tool.md" }
"""
    )
    assert tools_cmd.apply(target=tmp_path, tool_id="portable", force=True) == 0
    (tmp_path / "tools" / "source.md").write_text("Tool source changed\n")
    capsys.readouterr()

    assert tools_cmd.doctor(target=tmp_path) == 0
    out = capsys.readouterr().out
    assert "[warn] tool_unsafe_auth_fields: unsafe field names: password" in out
    assert "do-not-print" not in out
    assert "[warn] tool_invalid_schema:" in out
    assert "[warn] tool_stale_health:" in out
    assert "[warn] tool_missing_command: command is not resolvable: missing-command --flag" in out
    assert "[warn] tool_stale_projection:" in out
    assert "[warn] tool_parity_gap: missing projection for codex" in out

    assert tools_cmd.doctor(target=tmp_path, json_output=True) == 0
    payload = json.loads(capsys.readouterr().out)
    issue_types = {issue["issue_type"] for issue in payload["issues"]}
    assert {"unsafe_auth_fields", "invalid_schema", "stale_health", "missing_command", "stale_projection", "parity_gap"} <= issue_types
    rendered = json.dumps(payload, sort_keys=True)
    assert "do-not-print" not in rendered


def test_tools_import_issues_dedupes_and_respects_dismissed_until_change(tmp_path, capsys):
    _init_git_repo(tmp_path)
    config = tmp_path / ".brigade" / "tools.toml"
    config.parent.mkdir()
    config.write_text(
        """
[[tool]]
id = "portable"
name = "Portable Tool"
family = "skill"
enabled = true
description = "Portable missing source."
source_path = "tools/missing.md"
supported_harnesses = []
"""
    )

    assert tools_cmd.import_issues(target=tmp_path, json_output=True) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["created"] == 1
    item = payload["imports"][0]
    assert item["source"] == "tool-catalog"
    assert item["metadata"]["tool_id"] == "portable"
    assert item["metadata"]["tool_issue_type"] == "missing_source"

    assert tools_cmd.import_issues(target=tmp_path, json_output=True) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["created"] == 0
    assert payload["skipped"] == 1

    assert work_cmd.import_dismiss(target=tmp_path, import_id=item["id"], reason="ack") == 0
    capsys.readouterr()
    assert tools_cmd.import_issues(target=tmp_path, json_output=True) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["created"] == 0
    assert payload["dismissed"] == 1

    config.write_text(config.read_text().replace("tools/missing.md", "tools/changed.md"))
    assert tools_cmd.import_issues(target=tmp_path, json_output=True) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["created"] == 1


def test_work_brief_and_doctor_include_tool_catalog_health(tmp_path, monkeypatch, capsys):
    _init_git_repo(tmp_path)
    dogfood_cmd.init(target=tmp_path)
    monkeypatch.setattr(work_cmd.shutil, "which", lambda name: f"/usr/bin/{name}")
    monkeypatch.setattr(dogfood_cmd, "_check_git_ignored", lambda repo, path: "yes")
    source = tmp_path / "tools" / "portable.md"
    source.parent.mkdir()
    source.write_text("Portable source.\n")
    config = tmp_path / ".brigade" / "tools.toml"
    config.write_text(
        """
[[tool]]
id = "portable"
name = "Portable Tool"
family = "skill"
enabled = true
description = "Portable missing source."
source_path = "tools/portable.md"
supported_harnesses = ["codex"]
projections = { codex = ".codex/skills/portable/SKILL.md" }
"""
    )

    assert work_cmd.brief(target=tmp_path) == 0
    out = capsys.readouterr().out
    assert "tool_config:" in out
    assert "tool_catalog:" in out
    assert "tool_top_issue: portable/missing_projection" in out

    assert work_cmd.doctor(target=tmp_path) == 0
    out = capsys.readouterr().out
    assert "[warn] tool_missing_projection:" in out
    assert "[ok] tools_config_ignored: yes" in out


def test_tools_plan_and_apply_projection_lifecycle(tmp_path, capsys):
    _init_git_repo(tmp_path)
    source = tmp_path / "tools" / "simplify.md"
    source.parent.mkdir()
    source.write_text("Simplify the current task.\n")
    config = tmp_path / ".brigade" / "tools.toml"
    config.parent.mkdir()
    config.write_text(
        """
[[tool]]
id = "simplify"
name = "Simplify"
family = "slash-command"
enabled = true
description = "Portable simplify command."
source_path = "tools/simplify.md"
supported_harnesses = ["claude", "codex"]
projections = { claude = ".claude/commands/simplify.md", codex = ".codex/skills/simplify/SKILL.md" }
"""
    )

    assert tools_cmd.plan(target=tmp_path) == 0
    out = capsys.readouterr().out
    assert "tools projection plan:" in out
    assert "simplify claude missing action=create" in out

    assert tools_cmd.plan(target=tmp_path, tool_id="simplify", json_output=True) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["counts"]["missing"] == 2
    assert payload["projections"][0]["expected_fingerprint"]

    assert tools_cmd.apply(target=tmp_path, tool_id="simplify", dry_run=True, json_output=True) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["applied_count"] == 2
    assert not (tmp_path / ".claude" / "commands" / "simplify.md").exists()

    assert tools_cmd.apply(target=tmp_path, tool_id="simplify", json_output=True) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["applied_count"] == 2
    projection = tmp_path / ".claude" / "commands" / "simplify.md"
    text = projection.read_text()
    assert "brigade-tool-projection:" in text
    assert "Simplify the current task." in text
    metadata, body = tools_cmd._read_projection(projection)
    assert metadata["tool_id"] == "simplify"
    assert metadata["family"] == "slash-command"
    assert metadata["harness"] == "claude"
    assert metadata["source_fingerprint"]
    assert metadata["projection_fingerprint"] == tools_cmd._text_hash(body)

    assert tools_cmd.plan(target=tmp_path, json_output=True) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["counts"]["current"] == 2

    assert tools_cmd.apply(target=tmp_path, tool_id="simplify", json_output=True) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["applied_count"] == 0
    assert payload["skipped_count"] == 2

    source.write_text("Simplify the current task and remove duplication.\n")
    assert tools_cmd.plan(target=tmp_path, json_output=True) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["counts"]["stale"] == 2

    assert tools_cmd.apply(target=tmp_path, tool_id="simplify", json_output=True) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["applied_count"] == 2
    assert "remove duplication" in projection.read_text()

    projection.write_text(projection.read_text() + "\nlocal edit\n")
    assert tools_cmd.plan(target=tmp_path, tool_id="simplify", json_output=True) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["counts"]["conflicted"] == 1

    assert tools_cmd.apply(target=tmp_path, tool_id="simplify", json_output=True) == 1
    payload = json.loads(capsys.readouterr().out)
    assert payload["conflict_count"] == 1
    assert "local edit" in projection.read_text()

    assert tools_cmd.apply(target=tmp_path, tool_id="simplify", force=True, json_output=True) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["applied_count"] == 1
    assert "local edit" not in projection.read_text()


def test_tools_apply_creates_harness_script_and_mcp_projections(tmp_path, capsys):
    _init_git_repo(tmp_path)
    tools_dir = tmp_path / "tools"
    tools_dir.mkdir()
    (tools_dir / "super.md").write_text("Use shared superpowers.\n")
    (tools_dir / "script.sh").write_text("#!/bin/sh\necho ok\n")
    (tools_dir / "mcp.json").write_text('{"mcpServers":{"local":{"command":"brigade","timeout":10}}}\n')
    config = tmp_path / ".brigade" / "tools.toml"
    config.parent.mkdir()
    config.write_text(
        """
[[tool]]
id = "superpowers"
name = "Superpowers"
family = "superpower"
enabled = true
description = "Shared superpowers."
source_path = "tools/super.md"
supported_harnesses = ["claude", "codex", "opencode", "hermes", "openclaw", "mcp", "scripts"]
projections = { claude = ".claude/commands/superpowers.md", codex = ".codex/skills/superpowers/SKILL.md", opencode = ".opencode/superpowers/superpowers.md", hermes = ".hermes/superpowers/superpowers.md", openclaw = ".openclaw/superpowers/superpowers.md", mcp = ".mcp/superpowers.md", scripts = "scripts/superpowers.md" }

[[tool]]
id = "script-tool"
name = "Script Tool"
family = "script"
enabled = true
description = "Script projection."
source_path = "tools/script.sh"
command = "brigade status"
supported_harnesses = ["scripts"]
projections = { scripts = "scripts/script-tool.md" }

[[tool]]
id = "mcp-local"
name = "MCP Local"
family = "mcp"
enabled = true
description = "MCP projection."
source_path = "tools/mcp.json"
supported_harnesses = ["mcp"]
projections = { mcp = ".mcp/mcp-local.md" }
"""
    )

    assert tools_cmd.apply(target=tmp_path, all_tools=True, json_output=True) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["applied_count"] == 9
    for rel_path in (
        ".claude/commands/superpowers.md",
        ".codex/skills/superpowers/SKILL.md",
        ".opencode/superpowers/superpowers.md",
        ".hermes/superpowers/superpowers.md",
        ".openclaw/superpowers/superpowers.md",
        ".mcp/superpowers.md",
        "scripts/superpowers.md",
    ):
        assert (tmp_path / rel_path).is_file()
        assert "brigade-tool-projection:" in (tmp_path / rel_path).read_text()
    script_projection = (tmp_path / "scripts" / "script-tool.md").read_text()
    assert "Managed Brigade script projection." in script_projection
    assert "command: `brigade status`" in script_projection
    mcp_projection = (tmp_path / ".mcp" / "mcp-local.md").read_text()
    assert "Managed Brigade MCP projection stub." in mcp_projection
    assert "does not start MCP servers" in mcp_projection


def test_tools_apply_refuses_unmanaged_projection_unless_forced(tmp_path, capsys):
    _init_git_repo(tmp_path)
    source = tmp_path / "tools" / "simplify.md"
    source.parent.mkdir()
    source.write_text("Simplify source.\n")
    projection = tmp_path / ".claude" / "commands" / "simplify.md"
    projection.parent.mkdir(parents=True)
    projection.write_text("user managed projection\n")
    config = tmp_path / ".brigade" / "tools.toml"
    config.parent.mkdir()
    config.write_text(
        """
[[tool]]
id = "simplify"
name = "Simplify"
family = "slash-command"
enabled = true
description = "Portable simplify command."
source_path = "tools/simplify.md"
supported_harnesses = ["claude"]
projections = { claude = ".claude/commands/simplify.md" }
"""
    )

    assert tools_cmd.apply(target=tmp_path, tool_id="simplify", json_output=True) == 1
    payload = json.loads(capsys.readouterr().out)
    assert payload["conflict_count"] == 1
    assert payload["conflicts"][0]["status"] == "unmanaged"
    assert projection.read_text() == "user managed projection\n"

    assert tools_cmd.apply(target=tmp_path, tool_id="simplify", force=True, json_output=True) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["applied_count"] == 1
    assert "brigade-tool-projection:" in projection.read_text()
    assert "Simplify source." in projection.read_text()


def test_tools_doctor_and_import_issues_use_projection_states(tmp_path, capsys):
    _init_git_repo(tmp_path)
    source = tmp_path / "tools" / "simplify.md"
    source.parent.mkdir()
    source.write_text("Simplify source.\n")
    unmanaged = tmp_path / ".claude" / "commands" / "simplify.md"
    unmanaged.parent.mkdir(parents=True)
    unmanaged.write_text("unmanaged projection\n")
    config = tmp_path / ".brigade" / "tools.toml"
    config.parent.mkdir()
    config.write_text(
        """
[[tool]]
id = "simplify"
name = "Simplify"
family = "slash-command"
enabled = true
description = "Portable simplify command."
source_path = "tools/simplify.md"
supported_harnesses = ["claude", "codex"]
projections = { claude = ".claude/commands/simplify.md", codex = ".codex/skills/simplify/SKILL.md" }
"""
    )

    assert tools_cmd.doctor(target=tmp_path) == 0
    out = capsys.readouterr().out
    assert "[warn] tool_unmanaged_projection: claude: existing projection is not managed by Brigade" in out
    assert "[warn] tool_missing_projection: codex: projection will be created" in out

    assert tools_cmd.import_issues(target=tmp_path, json_output=True) == 0
    payload = json.loads(capsys.readouterr().out)
    issue_types = {item["metadata"]["tool_issue_type"] for item in payload["imports"]}
    assert {"unmanaged_projection", "missing_projection"} <= issue_types


def test_tools_describe_and_contracts_report_schema_contracts(tmp_path, capsys):
    _init_git_repo(tmp_path)
    tools_dir = tmp_path / "tools"
    tools_dir.mkdir()
    (tools_dir / "input.schema.json").write_text(
        json.dumps(
            {
                "type": "object",
                "required": ["path"],
                "properties": {
                    "path": {"type": "string"},
                    "mode": {"type": "string", "enum": ["fast", "safe"]},
                },
                "additionalProperties": False,
            }
        )
    )
    (tools_dir / "output.schema.json").write_text(
        json.dumps({"type": "object", "properties": {"ok": {"type": "boolean"}}})
    )
    (tools_dir / "examples.json").write_text("{}\n")
    config = tmp_path / ".brigade" / "tools.toml"
    config.parent.mkdir()
    config.write_text(
        """
[[tool]]
id = "script-tool"
name = "Script Tool"
family = "script"
enabled = true
description = "Contracted script."
command = "brigade status"
input_schema_path = "tools/input.schema.json"
output_schema_path = "tools/output.schema.json"
examples_path = "tools/examples.json"
permissions = ["read-files"]
effects = ["local-read"]
approval_mode = "on-request"
cwd = "."
env_labels = ["SAFE_ENV"]
argument_template = { path = "{path}", mode = "--mode={mode}" }
supported_harnesses = []
"""
    )

    assert tools_cmd.describe(target=tmp_path, tool_id="script-tool") == 0
    out = capsys.readouterr().out
    assert "tool: script-tool" in out
    assert "command: brigade status" in out
    assert "approval_mode: on-request" in out
    assert "permissions: read-files" in out

    assert tools_cmd.describe(target=tmp_path, tool_id="script-tool", json_output=True) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["tool"]["contract"]["has_contract"] is True
    assert payload["tool"]["contract"]["permissions"] == ["read-files"]
    assert payload["issue_count"] == 0

    assert tools_cmd.contracts(target=tmp_path) == 0
    out = capsys.readouterr().out
    assert "tools contracts:" in out
    assert "- script-tool [script] ready issues=0" in out

    assert tools_cmd.contracts(target=tmp_path, json_output=True) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["contract_count"] == 1
    assert payload["issue_count"] == 0


def test_tools_contracts_report_malformed_and_unsupported_schemas(tmp_path, capsys):
    _init_git_repo(tmp_path)
    tools_dir = tmp_path / "tools"
    tools_dir.mkdir()
    (tools_dir / "bad.schema.json").write_text("{not json")
    (tools_dir / "unsupported.schema.json").write_text(json.dumps({"type": "string"}))
    config = tmp_path / ".brigade" / "tools.toml"
    config.parent.mkdir()
    config.write_text(
        """
[[tool]]
id = "bad-contract"
name = "Bad Contract"
family = "script"
enabled = true
description = "Bad schema."
command = "brigade status"
input_schema_path = "tools/bad.schema.json"
output_schema_path = "tools/unsupported.schema.json"
examples_path = "tools/missing-examples.json"
argument_template = { "bad-key!" = "{path" }
supported_harnesses = []
"""
    )

    assert tools_cmd.doctor(target=tmp_path) == 0
    out = capsys.readouterr().out
    assert "[warn] tool_invalid_input_schema:" in out
    assert "[warn] tool_unsupported_output_schema:" in out
    assert "[warn] tool_missing_examples:" in out
    assert "[warn] tool_bad_argument_template:" in out

    assert tools_cmd.contracts(target=tmp_path, json_output=True) == 0
    payload = json.loads(capsys.readouterr().out)
    issue_types = {issue["issue_type"] for issue in payload["issues"]}
    assert {"invalid_input_schema", "unsupported_output_schema", "missing_examples", "bad_argument_template"} <= issue_types


def test_tools_call_plan_validates_args_and_renders_template(tmp_path, capsys):
    _init_git_repo(tmp_path)
    tools_dir = tmp_path / "tools"
    tools_dir.mkdir()
    (tools_dir / "input.schema.json").write_text(
        json.dumps(
            {
                "type": "object",
                "required": ["path", "count", "tags", "mode"],
                "properties": {
                    "path": {"type": "string"},
                    "count": {"type": "integer"},
                    "tags": {"type": "array", "items": {"type": "string"}},
                    "mode": {"type": "string", "enum": ["fast", "safe"]},
                },
                "additionalProperties": False,
            }
        )
    )
    config = tmp_path / ".brigade" / "tools.toml"
    config.parent.mkdir()
    config.write_text(
        """
[[tool]]
id = "runner"
name = "Runner"
family = "script"
enabled = true
description = "Call planner."
command = "brigade status"
input_schema_path = "tools/input.schema.json"
permissions = ["read-files"]
effects = ["local-read"]
approval_mode = "never"
auth_label = "local-safe"
env_labels = ["SAFE_ENV"]
argument_template = { target = "{path}", count = "--count={count}", mode = "--mode={mode}", tags = "{tags}" }
supported_harnesses = []
"""
    )
    args_path = tmp_path / "args.json"
    args_path.write_text(json.dumps({"path": "README.md", "count": 2, "tags": ["a", "b"], "mode": "safe"}))

    assert tools_cmd.call_plan(target=tmp_path, tool_id="runner", args_json=args_path, json_output=True) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["valid"] is True
    assert payload["plan"]["command"] == "brigade status"
    assert payload["plan"]["arguments"]["target"] == "README.md"
    assert payload["plan"]["arguments"]["count"] == "--count=2"
    assert payload["plan"]["arguments"]["mode"] == "--mode=safe"
    assert payload["plan"]["approval_required"] is False

    assert tools_cmd.call_plan(
        target=tmp_path,
        tool_id="runner",
        args='{"path":"README.md","count":"two","tags":["a", 1],"mode":"slow","extra":true}',
        json_output=True,
    ) == 1
    payload = json.loads(capsys.readouterr().out)
    blockers = "\n".join(payload["blockers"])
    assert "$.count: expected integer" in blockers
    assert "$.tags[1]: expected string" in blockers
    assert "$.mode: expected one of 'fast', 'safe'" in blockers
    assert "$.extra: additional property not allowed" in blockers


def test_tools_call_plan_redacts_and_reports_blockers(tmp_path, capsys):
    _init_git_repo(tmp_path)
    tools_dir = tmp_path / "tools"
    tools_dir.mkdir()
    (tools_dir / "blocked.md").write_text("Blocked source.\n")
    (tools_dir / "input.schema.json").write_text(json.dumps({"type": "object", "properties": {"token": {"type": "string"}}}))
    projection = tmp_path / ".claude" / "commands" / "blocked.md"
    projection.parent.mkdir(parents=True)
    projection.write_text("unmanaged\n")
    config = tmp_path / ".brigade" / "tools.toml"
    config.parent.mkdir()
    config.write_text(
        """
[[tool]]
id = "blocked"
name = "Blocked"
family = "script"
enabled = true
description = "Blocked plan."
source_path = "tools/blocked.md"
input_schema_path = "tools/input.schema.json"
auth_label = "api_token"
env_labels = ["SECRET_TOKEN"]
argument_template = { token = "{token}" }
supported_harnesses = ["claude"]
projections = { claude = ".claude/commands/blocked.md" }
"""
    )

    assert tools_cmd.call_plan(target=tmp_path, tool_id="blocked", args='{"token":"abc123"}', json_output=True) == 1
    payload = json.loads(capsys.readouterr().out)
    blockers = "\n".join(payload["blockers"])
    assert "command is required for call planning" in blockers
    assert "auth_label appears unsafe" in blockers
    assert "env label appears unsafe: SECRET_TOKEN" in blockers
    assert "one or more projections are conflicted or unmanaged" in blockers
    assert payload["plan"]["auth_label"] == "[redacted]"
    assert payload["plan"]["env_labels"] == ["[redacted]"]
    assert payload["plan"]["args"]["token"] == "[redacted]"
    rendered = json.dumps(payload, sort_keys=True)
    assert "abc123" not in rendered


def test_tools_import_issues_and_work_brief_surface_contract_health(tmp_path, monkeypatch, capsys):
    _init_git_repo(tmp_path)
    dogfood_cmd.init(target=tmp_path)
    capsys.readouterr()
    monkeypatch.setattr(work_cmd.shutil, "which", lambda name: f"/usr/bin/{name}")
    monkeypatch.setattr(dogfood_cmd, "_check_git_ignored", lambda repo, path: "yes")
    config = tmp_path / ".brigade" / "tools.toml"
    config.write_text(
        """
[[tool]]
id = "contractless"
name = "Contractless"
family = "script"
enabled = true
description = "Missing contract."
command = "brigade status"
supported_harnesses = []
"""
    )

    assert tools_cmd.import_issues(target=tmp_path, json_output=True) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["created"] == 1
    assert payload["imports"][0]["metadata"]["tool_issue_type"] == "missing_contract"

    assert work_cmd.brief(target=tmp_path) == 0
    out = capsys.readouterr().out
    assert "tool_top_issue: contractless/missing_contract" in out


def test_tools_call_queue_list_show_and_review_transitions(tmp_path, capsys):
    _init_git_repo(tmp_path)
    tools_dir = tmp_path / "tools"
    tools_dir.mkdir()
    (tools_dir / "input.schema.json").write_text(json.dumps({"type": "object", "properties": {"path": {"type": "string"}}}))
    config = tmp_path / ".brigade" / "tools.toml"
    config.parent.mkdir()
    config.write_text(
        """
[[tool]]
id = "runner"
name = "Runner"
family = "script"
enabled = true
description = "Queue runner."
command = "brigade status"
input_schema_path = "tools/input.schema.json"
permissions = ["read-files"]
effects = ["local-read"]
approval_mode = "on-request"
argument_template = { path = "{path}" }
supported_harnesses = []
"""
    )
    args_file = tmp_path / "args.json"
    args_file.write_text('{"path":"README.md"}\n')

    assert tools_cmd.call_queue(target=tmp_path, tool_id="runner", args_json=args_file, json_output=True) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["created"] == 1
    call_id = payload["call"]["id"]
    assert payload["call"]["status"] == "pending"
    assert payload["call"]["contract"]["approval_mode"] == "on-request"

    assert tools_cmd.call_queue(target=tmp_path, tool_id="runner", args='{"path":"README.md"}', json_output=True) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["skipped"] == 1
    assert "already pending" in payload["reason"]

    assert tools_cmd.call_list(target=tmp_path) == 0
    out = capsys.readouterr().out
    assert "tools call list:" in out
    assert "pending: 1" in out

    assert tools_cmd.call_list(target=tmp_path, json_output=True) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["counts"]["pending"] == 1

    assert tools_cmd.call_show(target=tmp_path, call_id=call_id[:12]) == 0
    out = capsys.readouterr().out
    assert f"call: {call_id}" in out
    assert "status: pending" in out

    assert tools_cmd.call_hold(target=tmp_path, call_id=call_id, reason="needs review", json_output=True) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["call"]["status"] == "held"
    assert payload["call"]["review_reason"] == "needs review"

    assert tools_cmd.call_reject(target=tmp_path, call_id=call_id, reason="not needed", json_output=True) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["call"]["status"] == "rejected"
    assert payload["call"]["review_reason"] == "not needed"


def test_tools_call_queue_blocked_requires_include_blocked_and_cannot_approve(tmp_path, capsys):
    _init_git_repo(tmp_path)
    config = tmp_path / ".brigade" / "tools.toml"
    config.parent.mkdir()
    config.write_text(
        """
[[tool]]
id = "blocked"
name = "Blocked"
family = "script"
enabled = true
description = "Blocked call."
command = "brigade status"
supported_harnesses = []
"""
    )

    assert tools_cmd.call_queue(target=tmp_path, tool_id="blocked", args="{}", json_output=True) == 1
    payload = json.loads(capsys.readouterr().out)
    assert payload["blocked"] == 1
    assert not (tmp_path / ".brigade" / "tools" / "calls.jsonl").exists()

    assert tools_cmd.call_queue(target=tmp_path, tool_id="blocked", args="{}", include_blocked=True, json_output=True) == 0
    payload = json.loads(capsys.readouterr().out)
    call_id = payload["call"]["id"]
    assert payload["call"]["blockers"]

    assert tools_cmd.call_approve(target=tmp_path, call_id=call_id, json_output=True) == 1
    payload = json.loads(capsys.readouterr().out)
    assert payload["error"] == "blocked calls cannot be approved"


def test_tools_call_queue_dedupes_and_requeues_after_change(tmp_path, capsys):
    _init_git_repo(tmp_path)
    tools_dir = tmp_path / "tools"
    tools_dir.mkdir()
    schema = tools_dir / "input.schema.json"
    schema.write_text(json.dumps({"type": "object", "properties": {"path": {"type": "string"}}}))
    config = tmp_path / ".brigade" / "tools.toml"
    config.parent.mkdir()
    config.write_text(
        """
[[tool]]
id = "runner"
name = "Runner"
family = "script"
enabled = true
description = "Queue runner."
command = "brigade status"
input_schema_path = "tools/input.schema.json"
argument_template = { path = "{path}" }
supported_harnesses = []
"""
    )

    assert tools_cmd.call_queue(target=tmp_path, tool_id="runner", args='{"path":"README.md"}', json_output=True) == 0
    first = json.loads(capsys.readouterr().out)["call"]
    assert tools_cmd.call_approve(target=tmp_path, call_id=first["id"], json_output=True) == 0
    capsys.readouterr()
    assert tools_cmd.call_queue(target=tmp_path, tool_id="runner", args='{"path":"README.md"}', json_output=True) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["skipped"] == 1
    assert "already approved" in payload["reason"]

    assert tools_cmd.call_queue(target=tmp_path, tool_id="runner", args='{"path":"CHANGELOG.md"}', json_output=True) == 0
    second = json.loads(capsys.readouterr().out)
    assert second["created"] == 1
    assert second["call"]["id"] != first["id"]
    assert tools_cmd.call_reject(target=tmp_path, call_id=second["call"]["id"], reason="bad timing", json_output=True) == 0
    capsys.readouterr()
    assert tools_cmd.call_queue(target=tmp_path, tool_id="runner", args='{"path":"CHANGELOG.md"}', json_output=True) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["skipped"] == 1
    assert "rejected" in payload["reason"]

    schema.write_text(json.dumps({"type": "object", "properties": {"path": {"type": "string"}, "mode": {"type": "string"}}}))
    assert tools_cmd.call_queue(target=tmp_path, tool_id="runner", args='{"path":"CHANGELOG.md"}', json_output=True) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["created"] == 1


def test_tools_call_queue_health_brief_and_import_issues(tmp_path, monkeypatch, capsys):
    _init_git_repo(tmp_path)
    dogfood_cmd.init(target=tmp_path)
    capsys.readouterr()
    monkeypatch.setattr(work_cmd.shutil, "which", lambda name: f"/usr/bin/{name}")
    monkeypatch.setattr(dogfood_cmd, "_check_git_ignored", lambda repo, path: "yes")
    tools_dir = tmp_path / "tools"
    tools_dir.mkdir()
    schema = tools_dir / "input.schema.json"
    schema.write_text(json.dumps({"type": "object", "properties": {"path": {"type": "string"}}}))
    config = tmp_path / ".brigade" / "tools.toml"
    config.write_text(
        """
[[tool]]
id = "runner"
name = "Runner"
family = "script"
enabled = true
description = "Queue runner."
command = "brigade status"
input_schema_path = "tools/input.schema.json"
argument_template = { path = "{path}" }
supported_harnesses = []
"""
    )
    now = datetime(2026, 5, 30, 12, 0, 0, tzinfo=timezone.utc)
    monkeypatch.setattr(tools_cmd, "_now", lambda: now)
    assert tools_cmd.call_queue(target=tmp_path, tool_id="runner", args='{"path":"README.md"}', json_output=True) == 0
    pending = json.loads(capsys.readouterr().out)["call"]
    calls = tools_cmd._read_calls(tmp_path)
    calls[0]["created_at"] = "2026-05-25T12:00:00+00:00"
    tools_cmd._write_calls(tmp_path, calls)

    assert tools_cmd.call_queue(target=tmp_path, tool_id="runner", args='{"path":"CHANGELOG.md"}', json_output=True) == 0
    approved = json.loads(capsys.readouterr().out)["call"]
    assert tools_cmd.call_approve(target=tmp_path, call_id=approved["id"], json_output=True) == 0
    capsys.readouterr()
    schema.write_text(json.dumps({"type": "object", "properties": {"path": {"type": "string"}, "mode": {"type": "string"}}}))

    assert tools_cmd.doctor(target=tmp_path) == 0
    out = capsys.readouterr().out
    assert "[warn] tool_call_stale_pending:" in out
    assert "[warn] tool_call_stale_approved:" in out

    assert work_cmd.brief(target=tmp_path) == 0
    out = capsys.readouterr().out
    assert "tool_call_pending:" in out
    assert "tool_call_top_issue:" in out
    assert pending["id"] in out

    assert tools_cmd.import_issues(target=tmp_path, json_output=True) == 0
    payload = json.loads(capsys.readouterr().out)
    issue_types = {item["metadata"]["tool_issue_type"] for item in payload["imports"]}
    assert {"call_stale_pending", "call_stale_approved"} <= issue_types


def _write_script_tool_config(tmp_path, *, script: str, timeout: float = 5.0) -> None:
    tools_dir = tmp_path / "tools"
    tools_dir.mkdir(exist_ok=True)
    (tools_dir / "runner.py").write_text(script)
    (tools_dir / "input.schema.json").write_text(
        json.dumps(
            {
                "type": "object",
                "properties": {"path": {"type": "string"}},
                "additionalProperties": True,
            }
        )
    )
    config = tmp_path / ".brigade" / "tools.toml"
    config.parent.mkdir(exist_ok=True)
    config.write_text(
        f"""
[[tool]]
id = "runner"
name = "Runner"
family = "script"
enabled = true
description = "Run local script."
command = "{sys.executable} tools/runner.py"
input_schema_path = "tools/input.schema.json"
timeout = {timeout}
permissions = ["read-files"]
effects = ["local-read"]
approval_mode = "on-request"
argument_template = {{ path = "{{path}}" }}
supported_harnesses = []
"""
    )


def _write_runtime_config(
    tmp_path,
    *,
    runtime_id="helper",
    command=None,
    health_command=None,
    health_path=None,
    cwd=".",
    port=None,
):
    command = command or f'{sys.executable} -c "import time; time.sleep(30)"'
    lines = [
        "[[runtime]]",
        f'id = "{runtime_id}"',
        'name = "Helper"',
        "enabled = true",
        f"command = {json.dumps(command)}",
        f"cwd = {json.dumps(cwd)}",
        f'pid_path = ".brigade/tools/runtime/{runtime_id}.pid"',
        f'log_path = ".brigade/tools/runtime/{runtime_id}.log"',
        "timeout = 2",
    ]
    if health_command is not None:
        lines.append(f"health_command = {json.dumps(health_command)}")
    if health_path is not None:
        lines.append(f"health_path = {json.dumps(health_path)}")
    if port is not None:
        lines.append(f"port = {port}")
    config = tmp_path / ".brigade" / "tools" / "runtimes.toml"
    config.parent.mkdir(parents=True, exist_ok=True)
    config.write_text("\n".join(lines) + "\n")


def _write_policy_config(
    tmp_path,
    *,
    allowed_families=None,
    allowed_effects=None,
    denied_effects=None,
    required_approval_modes=None,
    max_timeout=10,
    allowed_runtimes=None,
    env_bindings=None,
):
    allowed_families = ["script"] if allowed_families is None else allowed_families
    allowed_effects = ["local-read"] if allowed_effects is None else allowed_effects
    denied_effects = [] if denied_effects is None else denied_effects
    required_approval_modes = ["on-request", "always"] if required_approval_modes is None else required_approval_modes
    allowed_runtimes = [] if allowed_runtimes is None else allowed_runtimes
    env_bindings = {} if env_bindings is None else env_bindings
    config = tmp_path / ".brigade" / "tools" / "policy.toml"
    config.parent.mkdir(parents=True, exist_ok=True)
    config.write_text(
        "\n".join(
            [
                "allowed_families = " + json.dumps(allowed_families),
                "allowed_effects = " + json.dumps(allowed_effects),
                "denied_effects = " + json.dumps(denied_effects),
                "required_approval_modes = " + json.dumps(required_approval_modes),
                f"max_timeout = {max_timeout}",
                "allowed_runtimes = " + json.dumps(allowed_runtimes),
                "env_bindings = { "
                + ", ".join(f"{key} = {json.dumps(value)}" for key, value in env_bindings.items())
                + " }",
                "",
            ]
        )
    )


def _queue_and_approve_runner(tmp_path, capsys, args='{"path":"README.md"}'):
    assert tools_cmd.call_queue(target=tmp_path, tool_id="runner", args=args, json_output=True) == 0
    call = json.loads(capsys.readouterr().out)["call"]
    assert tools_cmd.call_approve(target=tmp_path, call_id=call["id"], json_output=True) == 0
    return json.loads(capsys.readouterr().out)["call"]


def test_tools_call_run_approved_script_writes_receipt_and_redacts_output(tmp_path, capsys):
    _init_git_repo(tmp_path)
    _write_script_tool_config(
        tmp_path,
        script='import sys\nprint("path=" + sys.argv[1])\nprint("api_token=secret-value")\n',
    )
    call = _queue_and_approve_runner(tmp_path, capsys)

    assert tools_cmd.call_run(target=tmp_path, call_id=call["id"], json_output=True) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["call"]["status"] == "completed"
    assert payload["call"]["exit_code"] == 0
    receipt = payload["receipt"]
    assert receipt["call_id"] == call["id"]
    assert receipt["status"] == "completed"
    assert receipt["exit_code"] == 0
    assert receipt["permissions"] == ["read-files"]
    assert receipt["effects"] == ["local-read"]
    assert receipt["stdout_summary"].startswith("path=README.md")
    assert "secret-value" not in json.dumps(payload)
    assert "api_token=[redacted]" in receipt["stdout_summary"]
    assert (tmp_path / ".brigade" / "tools" / "runs").is_dir()
    assert os.path.isfile(receipt["receipt_path"])
    assert os.path.isfile(receipt["stdout_log_path"])
    assert os.path.isfile(receipt["stderr_log_path"])

    assert tools_cmd.call_show(target=tmp_path, call_id=call["id"]) == 0
    out = capsys.readouterr().out
    assert "status: completed" in out


def test_tools_call_run_refuses_non_runnable_statuses_and_stale_records(tmp_path, capsys):
    _init_git_repo(tmp_path)
    _write_script_tool_config(tmp_path, script='print("ok")\n')

    assert tools_cmd.call_queue(target=tmp_path, tool_id="runner", args='{"path":"pending"}', json_output=True) == 0
    pending = json.loads(capsys.readouterr().out)["call"]
    assert tools_cmd.call_run(target=tmp_path, call_id=pending["id"], json_output=True) == 1
    assert "must be approved" in " ".join(json.loads(capsys.readouterr().out)["blockers"])

    rejected = _queue_and_approve_runner(tmp_path, capsys, args='{"path":"rejected"}')
    assert tools_cmd.call_reject(target=tmp_path, call_id=rejected["id"], reason="no", json_output=True) == 0
    capsys.readouterr()
    assert tools_cmd.call_run(target=tmp_path, call_id=rejected["id"], json_output=True) == 1
    assert "must be approved" in " ".join(json.loads(capsys.readouterr().out)["blockers"])

    held = _queue_and_approve_runner(tmp_path, capsys, args='{"path":"held"}')
    assert tools_cmd.call_hold(target=tmp_path, call_id=held["id"], reason="wait", json_output=True) == 0
    capsys.readouterr()
    assert tools_cmd.call_run(target=tmp_path, call_id=held["id"], json_output=True) == 1
    assert "must be approved" in " ".join(json.loads(capsys.readouterr().out)["blockers"])

    blocked_config = tmp_path / ".brigade" / "tools.toml"
    blocked_config.write_text(
        f"""
[[tool]]
id = "blocked"
name = "Blocked"
family = "script"
enabled = true
description = "Blocked."
command = "{sys.executable} tools/runner.py"
supported_harnesses = []
"""
    )
    assert tools_cmd.call_queue(target=tmp_path, tool_id="blocked", args="{}", include_blocked=True, json_output=True) == 0
    blocked = json.loads(capsys.readouterr().out)["call"]
    calls = tools_cmd._read_calls(tmp_path)
    for item in calls:
        if item["id"] == blocked["id"]:
            item["status"] = "approved"
            item["reviewed_at"] = "2026-05-27T12:00:00+00:00"
            item["approval_fingerprint"] = tools_cmd._approval_fingerprint(item)
    tools_cmd._write_calls(tmp_path, calls)
    assert tools_cmd.call_run(target=tmp_path, call_id=blocked["id"], json_output=True) == 1
    assert "blocked calls cannot be run" in " ".join(json.loads(capsys.readouterr().out)["blockers"])

    _write_script_tool_config(tmp_path, script='print("ok")\n')
    stale = _queue_and_approve_runner(tmp_path, capsys, args='{"path":"stale"}')
    (tmp_path / "tools" / "input.schema.json").write_text(
        json.dumps({"type": "object", "properties": {"path": {"type": "string"}, "mode": {"type": "string"}}})
    )
    assert tools_cmd.call_run(target=tmp_path, call_id=stale["id"], json_output=True) == 1
    assert "contract fingerprint is stale" in " ".join(json.loads(capsys.readouterr().out)["blockers"])

    _write_script_tool_config(tmp_path, script='print("ok")\n')
    completed = _queue_and_approve_runner(tmp_path, capsys, args='{"path":"completed"}')
    assert tools_cmd.call_run(target=tmp_path, call_id=completed["id"], json_output=True) == 0
    capsys.readouterr()
    assert tools_cmd.call_run(target=tmp_path, call_id=completed["id"], json_output=True) == 1
    assert "completed calls cannot be run again" in " ".join(json.loads(capsys.readouterr().out)["blockers"])


def test_tools_call_run_next_failure_timeout_health_and_imports(tmp_path, monkeypatch, capsys):
    _init_git_repo(tmp_path)
    dogfood_cmd.init(target=tmp_path)
    capsys.readouterr()
    monkeypatch.setattr(work_cmd.shutil, "which", lambda name: f"/usr/bin/{name}")
    monkeypatch.setattr(dogfood_cmd, "_check_git_ignored", lambda repo, path: "yes")
    _write_script_tool_config(
        tmp_path,
        script='import sys\nprint("api_token=secret-value")\nsys.exit(7)\n',
    )
    failed = _queue_and_approve_runner(tmp_path, capsys, args='{"path":"failed"}')

    assert tools_cmd.call_run(target=tmp_path, next_call=True, json_output=True) == 1
    payload = json.loads(capsys.readouterr().out)
    assert payload["call"]["id"] == failed["id"]
    assert payload["call"]["status"] == "failed"
    assert payload["receipt"]["exit_code"] == 7
    assert "secret-value" not in json.dumps(payload)

    _write_script_tool_config(
        tmp_path,
        script='import time\ntime.sleep(3)\n',
        timeout=0.1,
    )
    timed = _queue_and_approve_runner(tmp_path, capsys, args='{"path":"timed"}')
    assert tools_cmd.call_run(target=tmp_path, call_id=timed["id"], json_output=True) == 1
    payload = json.loads(capsys.readouterr().out)
    assert payload["call"]["status"] == "failed"
    assert payload["receipt"]["timed_out"] is True

    calls = tools_cmd._read_calls(tmp_path)
    running = dict(calls[-1])
    running["id"] = "call-running-stale"
    running["status"] = "running"
    running["started_at"] = "2026-05-25T12:00:00+00:00"
    running["completed_at"] = None
    calls.append(running)
    tools_cmd._write_calls(tmp_path, calls)
    monkeypatch.setattr(tools_cmd, "_now", lambda: datetime(2026, 5, 30, 12, 0, 0, tzinfo=timezone.utc))

    assert tools_cmd.doctor(target=tmp_path) == 0
    out = capsys.readouterr().out
    assert "[warn] tool_call_failed:" in out
    assert "[warn] tool_call_running_stale:" in out

    assert work_cmd.brief(target=tmp_path) == 0
    out = capsys.readouterr().out
    assert "tool_call_top_issue:" in out
    assert "call_failed" in out

    assert tools_cmd.import_issues(target=tmp_path, json_output=True) == 0
    payload = json.loads(capsys.readouterr().out)
    issue_types = {item["metadata"]["tool_issue_type"] for item in payload["imports"]}
    assert {"call_failed", "call_running_stale"} <= issue_types


def test_tools_run_history_list_show_latest_and_json(tmp_path, capsys):
    _init_git_repo(tmp_path)
    _write_script_tool_config(tmp_path, script='import sys\nprint("ran=" + sys.argv[1])\n')
    call = _queue_and_approve_runner(tmp_path, capsys)

    assert tools_cmd.call_run(target=tmp_path, call_id=call["id"], json_output=True) == 0
    run_payload = json.loads(capsys.readouterr().out)
    run_id = run_payload["receipt"]["id"]

    assert tools_cmd.run_list(target=tmp_path) == 0
    out = capsys.readouterr().out
    assert "tools run list:" in out
    assert f"- {run_id} [completed] runner exit_code=0" in out

    assert tools_cmd.run_list(target=tmp_path, json_output=True) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["run_count"] == 1
    assert payload["runs"][0]["id"] == run_id
    assert payload["runs"][0]["stdout_summary"] == "ran=README.md"

    assert tools_cmd.run_show(target=tmp_path, run_id=run_id[:12]) == 0
    out = capsys.readouterr().out
    assert f"run: {run_id}" in out
    assert "status: completed" in out
    assert "stdout_log_path:" in out

    assert tools_cmd.run_show(target=tmp_path, run_id=run_id, json_output=True) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["run"]["id"] == run_id
    assert payload["run"]["call_id"] == call["id"]

    assert tools_cmd.run_latest(target=tmp_path, json_output=True) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["run"]["id"] == run_id


def test_tools_run_history_malformed_receipt_and_missing_log_warnings(tmp_path, capsys):
    _init_git_repo(tmp_path)
    _write_script_tool_config(tmp_path, script='print("ok")\n')
    call = _queue_and_approve_runner(tmp_path, capsys)
    assert tools_cmd.call_run(target=tmp_path, call_id=call["id"], json_output=True) == 0
    payload = json.loads(capsys.readouterr().out)
    Path(payload["receipt"]["stdout_log_path"]).unlink()
    runs_dir = tmp_path / ".brigade" / "tools" / "runs"
    (runs_dir / "bad.json").write_text("{not json")

    assert tools_cmd.run_list(target=tmp_path, json_output=True) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["error_count"] == 1

    assert tools_cmd.doctor(target=tmp_path) == 0
    out = capsys.readouterr().out
    assert "[warn] tool_run_receipt_invalid:" in out
    assert "[warn] tool_run_missing_log:" in out


def test_tools_run_replay_creates_pending_call_without_execution(tmp_path, capsys):
    _init_git_repo(tmp_path)
    marker = tmp_path / "marker.txt"
    _write_script_tool_config(
        tmp_path,
        script='from pathlib import Path\nPath("marker.txt").write_text(Path("marker.txt").read_text() + "x" if Path("marker.txt").exists() else "x")\n',
    )
    call = _queue_and_approve_runner(tmp_path, capsys)
    assert tools_cmd.call_run(target=tmp_path, call_id=call["id"], json_output=True) == 0
    run_id = json.loads(capsys.readouterr().out)["receipt"]["id"]
    assert marker.read_text() == "x"

    assert tools_cmd.run_replay(target=tmp_path, run_id=run_id, json_output=True) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["created"] == 1
    assert payload["executed"] == 0
    assert payload["call"]["status"] == "pending"
    assert payload["call"]["replay_of_run_id"] == run_id
    assert marker.read_text() == "x"

    calls = tools_cmd._read_calls(tmp_path)
    replay_calls = [item for item in calls if item.get("replay_of_run_id") == run_id]
    assert len(replay_calls) == 1
    assert replay_calls[0]["id"] != call["id"]


def test_tools_run_replay_blocks_stale_policy_state(tmp_path, capsys):
    _init_git_repo(tmp_path)
    _write_script_tool_config(tmp_path, script='print("ok")\n')
    _write_policy_config(tmp_path)
    call = _queue_and_approve_runner(tmp_path, capsys)
    assert tools_cmd.call_run(target=tmp_path, call_id=call["id"], json_output=True) == 0
    run_id = json.loads(capsys.readouterr().out)["receipt"]["id"]

    _write_policy_config(tmp_path, denied_effects=["local-read"])
    assert tools_cmd.run_replay(target=tmp_path, run_id=run_id, json_output=True) == 1
    payload = json.loads(capsys.readouterr().out)
    assert payload["created"] == 0
    assert "effect is denied by policy: local-read" in "\n".join(payload["blockers"])


def test_tools_run_replay_does_not_recover_secret_env_values(tmp_path, monkeypatch, capsys):
    _init_git_repo(tmp_path)
    secret_value = "super-secret-value"
    monkeypatch.setenv("BRIGADE_TEST_SECRET", secret_value)
    _write_script_tool_config(
        tmp_path,
        script='import os\nprint("secret=" + os.environ.get("SAFE_LABEL", ""))\n',
    )
    config = tmp_path / ".brigade" / "tools.toml"
    config.write_text(config.read_text() + 'env_labels = ["SAFE_LABEL"]\n')
    _write_policy_config(tmp_path, env_bindings={"SAFE_LABEL": "BRIGADE_TEST_SECRET"})
    call = _queue_and_approve_runner(tmp_path, capsys, args='{"path":"README.md","api_token":"argument-secret"}')
    assert tools_cmd.call_run(target=tmp_path, call_id=call["id"], json_output=True) == 0
    payload = json.loads(capsys.readouterr().out)
    run_id = payload["receipt"]["id"]
    assert secret_value not in json.dumps(payload)
    assert payload["receipt"]["args"]["api_token"] == "[redacted]"

    assert tools_cmd.run_replay(target=tmp_path, run_id=run_id, json_output=True) == 0
    replay_payload = json.loads(capsys.readouterr().out)
    rendered = json.dumps(replay_payload)
    assert secret_value not in rendered
    assert replay_payload["call"]["args"]["api_token"] == "[redacted]"
    assert "argument-secret" not in rendered
    assert secret_value not in (tmp_path / ".brigade" / "tools" / "calls.jsonl").read_text()
    assert secret_value not in Path(payload["receipt"]["receipt_path"]).read_text()


def test_tools_run_history_integrates_with_brief_and_imports(tmp_path, monkeypatch, capsys):
    _init_git_repo(tmp_path)
    dogfood_cmd.init(target=tmp_path)
    capsys.readouterr()
    monkeypatch.setattr(work_cmd.shutil, "which", lambda name: f"/usr/bin/{name}")
    monkeypatch.setattr(dogfood_cmd, "_check_git_ignored", lambda repo, path: "yes")
    _write_script_tool_config(tmp_path, script='import sys\nsys.exit(6)\n')
    failed = _queue_and_approve_runner(tmp_path, capsys)

    assert tools_cmd.call_run(target=tmp_path, call_id=failed["id"], json_output=True) == 1
    run_id = json.loads(capsys.readouterr().out)["receipt"]["id"]

    assert work_cmd.brief(target=tmp_path) == 0
    out = capsys.readouterr().out
    assert "tool_run_top_issue:" in out
    assert run_id in out

    assert tools_cmd.import_issues(target=tmp_path, json_output=True) == 0
    payload = json.loads(capsys.readouterr().out)
    issue_types = {item["metadata"]["tool_issue_type"] for item in payload["imports"]}
    assert "run_failed" in issue_types
    imported = [item for item in payload["imports"] if item["metadata"]["tool_issue_type"] == "run_failed"][0]
    assert imported["metadata"]["tool_run_id"] == run_id


def _checkpoint_script(*, fail_on_resume: bool = False) -> str:
    resume_failure = "sys.exit(5)" if fail_on_resume else ""
    return f"""
import json
import os
import sys
from pathlib import Path

checkpoint_dir = Path(os.environ["BRIGADE_TOOL_CHECKPOINT_DIR"])
checkpoint_dir.mkdir(parents=True, exist_ok=True)
if os.environ.get("BRIGADE_TOOL_RESUME_CHECKPOINT_ID"):
    print("resumed choice=" + os.environ.get("BRIGADE_TOOL_RESUME_CHOICE", ""))
    Path("resumed.txt").write_text(os.environ.get("BRIGADE_TOOL_RESUME_CHOICE", ""))
    {resume_failure}
else:
    (checkpoint_dir / "request.json").write_text(json.dumps({{
        "reason": "needs operator review",
        "requested_action": "choose next step",
        "prompt": "Continue with token=prompt-secret?",
        "context": {{"api_token": "argument-secret", "note": "secret=private-value"}},
        "choices": ["continue", "abort"]
    }}))
    print("checkpoint requested")
"""


def _create_waiting_checkpoint(tmp_path, capsys, *, script: str | None = None, args='{"path":"README.md"}'):
    _write_script_tool_config(tmp_path, script=script or _checkpoint_script())
    call = _queue_and_approve_runner(tmp_path, capsys, args=args)
    assert tools_cmd.call_run(target=tmp_path, call_id=call["id"], json_output=True) == 0
    payload = json.loads(capsys.readouterr().out)
    checkpoint_id = payload["receipt"]["checkpoint_id"]
    return payload["call"], checkpoint_id, payload["receipt"]


def test_tools_checkpoint_creation_list_show_and_redaction(tmp_path, capsys):
    _init_git_repo(tmp_path)
    call, checkpoint_id, receipt = _create_waiting_checkpoint(tmp_path, capsys)
    assert call["status"] == "waiting"
    assert receipt["status"] == "waiting"
    assert receipt["checkpoint"]["id"] == checkpoint_id
    assert receipt["checkpoint"]["context"]["api_token"] == "[redacted]"
    assert "prompt-secret" not in json.dumps(receipt)
    assert "argument-secret" not in json.dumps(receipt)
    assert "private-value" not in json.dumps(receipt)

    assert tools_cmd.checkpoint_list(target=tmp_path) == 0
    out = capsys.readouterr().out
    assert "tools checkpoint list:" in out
    assert f"- {checkpoint_id} [pending] runner choose next step" in out

    assert tools_cmd.checkpoint_list(target=tmp_path, json_output=True) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["checkpoint_count"] == 1
    assert payload["checkpoints"][0]["id"] == checkpoint_id

    assert tools_cmd.checkpoint_show(target=tmp_path, checkpoint_id=checkpoint_id[:12]) == 0
    out = capsys.readouterr().out
    assert f"checkpoint: {checkpoint_id}" in out
    assert "choices: continue, abort" in out

    assert tools_cmd.checkpoint_show(target=tmp_path, checkpoint_id=checkpoint_id, json_output=True) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["checkpoint"]["context"]["note"] == "secret=[redacted]"


def test_tools_checkpoint_approve_reject_and_successful_resume(tmp_path, capsys):
    _init_git_repo(tmp_path)
    call, checkpoint_id, receipt = _create_waiting_checkpoint(tmp_path, capsys)

    assert tools_cmd.checkpoint_approve(target=tmp_path, checkpoint_id=checkpoint_id, choice="continue", json_output=True) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["checkpoint"]["status"] == "approved"
    assert payload["checkpoint"]["selected_choice"] == "continue"
    assert payload["call"]["status"] == "resume-pending"

    assert tools_cmd.checkpoint_resume(target=tmp_path, checkpoint_id=checkpoint_id, json_output=True) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["checkpoint"]["status"] == "resumed"
    assert payload["call"]["status"] == "resumed"
    assert payload["receipt"]["status"] == "resumed"
    assert payload["receipt"]["original_call_id"] == call["id"]
    assert payload["receipt"]["original_run_id"] == receipt["id"]
    assert payload["receipt"]["checkpoint_id"] == checkpoint_id
    assert payload["receipt"]["resume_run_id"] == payload["receipt"]["id"]
    assert (tmp_path / "resumed.txt").read_text() == "continue"

    _write_script_tool_config(tmp_path, script=_checkpoint_script())
    second = _queue_and_approve_runner(tmp_path, capsys, args='{"path":"other"}')
    assert tools_cmd.call_run(target=tmp_path, call_id=second["id"], json_output=True) == 0
    second_checkpoint = json.loads(capsys.readouterr().out)["receipt"]["checkpoint_id"]
    assert tools_cmd.checkpoint_reject(target=tmp_path, checkpoint_id=second_checkpoint, reason="not now", json_output=True) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["checkpoint"]["status"] == "rejected"
    assert payload["checkpoint"]["review_reason"] == "not now"


def test_tools_checkpoint_resume_refuses_unapproved_expired_stale_blocked_and_policy_denied(tmp_path, capsys):
    _init_git_repo(tmp_path)
    _, checkpoint_id, _ = _create_waiting_checkpoint(tmp_path, capsys)
    assert tools_cmd.checkpoint_resume(target=tmp_path, checkpoint_id=checkpoint_id, json_output=True) == 1
    payload = json.loads(capsys.readouterr().out)
    assert "checkpoint must be approved before resume" in "\n".join(payload["blockers"])

    assert tools_cmd.checkpoint_approve(target=tmp_path, checkpoint_id=checkpoint_id, choice="continue", json_output=True) == 0
    capsys.readouterr()
    checkpoint, _ = tools_cmd._resolve_checkpoint(tmp_path, checkpoint_id)
    assert checkpoint is not None
    checkpoint["expires_at"] = "2026-05-01T00:00:00+00:00"
    tools_cmd._write_checkpoint(tmp_path, checkpoint)
    assert tools_cmd.checkpoint_resume(target=tmp_path, checkpoint_id=checkpoint_id, json_output=True) == 1
    assert "checkpoint is expired" in "\n".join(json.loads(capsys.readouterr().out)["blockers"])

    _write_script_tool_config(tmp_path, script=_checkpoint_script())
    _, stale_checkpoint, _ = _create_waiting_checkpoint(tmp_path, capsys, args='{"path":"stale"}')
    assert tools_cmd.checkpoint_approve(target=tmp_path, checkpoint_id=stale_checkpoint, choice="continue", json_output=True) == 0
    capsys.readouterr()
    (tmp_path / "tools" / "input.schema.json").write_text(
        json.dumps({"type": "object", "properties": {"path": {"type": "string"}, "mode": {"type": "string"}}})
    )
    assert tools_cmd.checkpoint_resume(target=tmp_path, checkpoint_id=stale_checkpoint, json_output=True) == 1
    blockers = "\n".join(json.loads(capsys.readouterr().out)["blockers"])
    assert "contract fingerprint is stale" in blockers

    _write_script_tool_config(tmp_path, script=_checkpoint_script())
    _, blocked_checkpoint, _ = _create_waiting_checkpoint(tmp_path, capsys, args='{"path":"blocked"}')
    assert tools_cmd.checkpoint_approve(target=tmp_path, checkpoint_id=blocked_checkpoint, choice="continue", json_output=True) == 0
    capsys.readouterr()
    calls = tools_cmd._read_calls(tmp_path)
    for item in calls:
        if item.get("checkpoint_id") == blocked_checkpoint:
            item["blockers"] = ["manual blocker"]
            item["approval_fingerprint"] = tools_cmd._approval_fingerprint(item)
    tools_cmd._write_calls(tmp_path, calls)
    assert tools_cmd.checkpoint_resume(target=tmp_path, checkpoint_id=blocked_checkpoint, json_output=True) == 1
    assert "blocked calls cannot be run" in "\n".join(json.loads(capsys.readouterr().out)["blockers"])

    _write_script_tool_config(tmp_path, script=_checkpoint_script())
    _write_policy_config(tmp_path)
    _, policy_checkpoint, _ = _create_waiting_checkpoint(tmp_path, capsys, args='{"path":"policy"}')
    _write_policy_config(tmp_path, denied_effects=["local-read"])
    assert tools_cmd.checkpoint_approve(target=tmp_path, checkpoint_id=policy_checkpoint, choice="continue", json_output=True) == 0
    capsys.readouterr()
    assert tools_cmd.checkpoint_resume(target=tmp_path, checkpoint_id=policy_checkpoint, json_output=True) == 1
    assert "effect is denied by policy: local-read" in "\n".join(json.loads(capsys.readouterr().out)["blockers"])


def test_tools_checkpoint_resume_failure_health_brief_and_imports(tmp_path, monkeypatch, capsys):
    _init_git_repo(tmp_path)
    dogfood_cmd.init(target=tmp_path)
    capsys.readouterr()
    monkeypatch.setattr(work_cmd.shutil, "which", lambda name: f"/usr/bin/{name}")
    monkeypatch.setattr(dogfood_cmd, "_check_git_ignored", lambda repo, path: "yes")
    _, checkpoint_id, _ = _create_waiting_checkpoint(tmp_path, capsys, script=_checkpoint_script(fail_on_resume=True))
    assert tools_cmd.checkpoint_approve(target=tmp_path, checkpoint_id=checkpoint_id, choice="continue", json_output=True) == 0
    capsys.readouterr()
    assert tools_cmd.checkpoint_resume(target=tmp_path, checkpoint_id=checkpoint_id, json_output=True) == 1
    payload = json.loads(capsys.readouterr().out)
    assert payload["checkpoint"]["status"] == "failed"
    assert payload["receipt"]["status"] == "failed"

    assert tools_cmd.doctor(target=tmp_path) == 0
    out = capsys.readouterr().out
    assert "[warn] tool_checkpoint_failed:" in out

    assert work_cmd.brief(target=tmp_path) == 0
    out = capsys.readouterr().out
    assert "tool_checkpoint_top_issue:" in out
    assert checkpoint_id in out

    assert tools_cmd.import_issues(target=tmp_path, json_output=True) == 0
    payload = json.loads(capsys.readouterr().out)
    issue_types = {item["metadata"]["tool_issue_type"] for item in payload["imports"]}
    assert "checkpoint_failed" in issue_types
    imported = [item for item in payload["imports"] if item["metadata"]["tool_issue_type"] == "checkpoint_failed"][0]
    assert imported["metadata"]["tool_checkpoint_id"] == checkpoint_id


def test_tools_checkpoint_does_not_store_secret_env_values(tmp_path, monkeypatch, capsys):
    _init_git_repo(tmp_path)
    secret_value = "super-secret-value"
    monkeypatch.setenv("BRIGADE_TEST_SECRET", secret_value)
    _write_script_tool_config(
        tmp_path,
        script="""
import json
import os
from pathlib import Path

checkpoint_dir = Path(os.environ["BRIGADE_TOOL_CHECKPOINT_DIR"])
checkpoint_dir.mkdir(parents=True, exist_ok=True)
if os.environ.get("BRIGADE_TOOL_RESUME_CHECKPOINT_ID"):
    print("secret=" + os.environ.get("SAFE_LABEL", ""))
else:
    (checkpoint_dir / "request.json").write_text(json.dumps({
        "reason": "needs secret-safe review",
        "requested_action": "continue",
        "prompt": "secret=" + os.environ.get("SAFE_LABEL", ""),
        "context": {"secret": os.environ.get("SAFE_LABEL", ""), "api_token": "argument-secret"},
        "choices": ["continue"]
    }))
""",
    )
    config = tmp_path / ".brigade" / "tools.toml"
    config.write_text(config.read_text() + 'env_labels = ["SAFE_LABEL"]\n')
    _write_policy_config(tmp_path, env_bindings={"SAFE_LABEL": "BRIGADE_TEST_SECRET"})
    call = _queue_and_approve_runner(tmp_path, capsys, args='{"path":"README.md","api_token":"argument-secret"}')
    assert tools_cmd.call_run(target=tmp_path, call_id=call["id"], json_output=True) == 0
    payload = json.loads(capsys.readouterr().out)
    checkpoint_id = payload["receipt"]["checkpoint_id"]
    rendered = json.dumps(payload)
    assert secret_value not in rendered
    assert "argument-secret" not in rendered

    assert tools_cmd.checkpoint_approve(target=tmp_path, checkpoint_id=checkpoint_id, choice="continue", json_output=True) == 0
    capsys.readouterr()
    assert tools_cmd.checkpoint_resume(target=tmp_path, checkpoint_id=checkpoint_id, json_output=True) == 0
    payload = json.loads(capsys.readouterr().out)
    rendered = json.dumps(payload)
    assert secret_value not in rendered
    assert "argument-secret" not in rendered
    assert secret_value not in Path(payload["receipt"]["receipt_path"]).read_text()
    assert secret_value not in Path(payload["receipt"]["stdout_log_path"]).read_text()


def _write_mcp_tool_config(tmp_path, *, server_script: str, timeout: float = 5.0) -> None:
    tools_dir = tmp_path / "tools"
    tools_dir.mkdir(exist_ok=True)
    (tools_dir / "fake_mcp.py").write_text(server_script)
    (tools_dir / "mcp-input.schema.json").write_text(
        json.dumps(
            {
                "type": "object",
                "properties": {"path": {"type": "string"}, "api_token": {"type": "string"}},
                "additionalProperties": True,
            }
        )
    )
    config = tmp_path / ".brigade" / "tools.toml"
    config.parent.mkdir(exist_ok=True)
    config.write_text(
        f"""
[[tool]]
id = "mcp-runner"
name = "MCP Runner"
family = "mcp"
enabled = true
description = "Run local MCP tool."
command = "{sys.executable} tools/fake_mcp.py"
input_schema_path = "tools/mcp-input.schema.json"
timeout = {timeout}
permissions = ["read-files"]
effects = ["local-read"]
approval_mode = "on-request"
runtime_id = "helper"
requires_runtime = true
mcp_server_id = "helper"
mcp_tool_name = "echo"
supported_harnesses = []
"""
    )


def _fake_mcp_server_script(*, malformed: bool = False, sleep_seconds: float = 0.0, copy_env: bool = False) -> str:
    if malformed:
        return 'print("not-json", flush=True)\n'
    env_line = '" env=" + os.environ.get("SAFE_LABEL", "")' if copy_env else '""'
    return f"""
import json
import os
import sys
import time
from pathlib import Path

time.sleep({sleep_seconds})
methods = []
for line in sys.stdin:
    if not line.strip():
        continue
    request = json.loads(line)
    methods.append(request.get("method", ""))
    method = request.get("method")
    if method == "initialize":
        response = {{"jsonrpc": "2.0", "id": request.get("id"), "result": {{"protocolVersion": "2024-11-05", "capabilities": {{}}}}}}
    elif method == "tools/list":
        response = {{"jsonrpc": "2.0", "id": request.get("id"), "result": {{"tools": [{{"name": "echo", "inputSchema": {{"type": "object"}}}}]}}}}
    elif method == "tools/call":
        arguments = request.get("params", {{}}).get("arguments", {{}})
        text = "echo " + str(arguments.get("path", "")) + " api_token=server-secret" + ({env_line})
        response = {{"jsonrpc": "2.0", "id": request.get("id"), "result": {{"content": [{{"type": "text", "text": text}}]}}}}
    else:
        response = {{"jsonrpc": "2.0", "id": request.get("id"), "error": {{"code": -32601, "message": "unknown"}}}}
    print(json.dumps(response), flush=True)
Path("mcp-methods.json").write_text(json.dumps(methods))
"""


def _queue_and_approve_mcp(tmp_path, capsys, args='{"path":"README.md"}'):
    assert tools_cmd.call_queue(target=tmp_path, tool_id="mcp-runner", args=args, json_output=True) == 0
    call = json.loads(capsys.readouterr().out)["call"]
    assert tools_cmd.call_approve(target=tmp_path, call_id=call["id"], json_output=True) == 0
    return json.loads(capsys.readouterr().out)["call"]


def test_tools_call_run_approved_mcp_stdio_writes_receipt_and_message_flow(tmp_path, capsys):
    _init_git_repo(tmp_path)
    _write_mcp_tool_config(tmp_path, server_script=_fake_mcp_server_script())
    _write_runtime_config(tmp_path)
    _write_policy_config(tmp_path, allowed_families=["mcp"], allowed_runtimes=["helper"])
    assert tools_cmd.runtime_start(target=tmp_path, runtime_id="helper", json_output=True) == 0
    capsys.readouterr()
    try:
        call = _queue_and_approve_mcp(tmp_path, capsys)
        assert tools_cmd.call_run(target=tmp_path, call_id=call["id"], json_output=True) == 0
        payload = json.loads(capsys.readouterr().out)
        receipt = payload["receipt"]
        assert payload["call"]["status"] == "completed"
        assert receipt["family"] == "mcp"
        assert receipt["mcp_server_id"] == "helper"
        assert receipt["mcp_tool_name"] == "echo"
        assert receipt["mcp_request_id"] == 3
        assert receipt["mcp_request_payload"]["method"] == "tools/call"
        assert receipt["mcp_request_payload"]["params"]["name"] == "echo"
        assert receipt["mcp_response_summary"]["result"]["content"][0]["text"].startswith("echo README.md")
        assert json.loads((tmp_path / "mcp-methods.json").read_text()) == ["initialize", "tools/list", "tools/call"]
    finally:
        tools_cmd.runtime_stop(target=tmp_path, runtime_id="helper", json_output=True)
        capsys.readouterr()


def test_tools_call_run_refuses_bad_mcp_status_policy_and_runtime(tmp_path, capsys):
    _init_git_repo(tmp_path)
    _write_mcp_tool_config(tmp_path, server_script=_fake_mcp_server_script())
    _write_runtime_config(tmp_path)
    _write_policy_config(tmp_path, allowed_families=["mcp"], allowed_runtimes=["helper"])

    assert tools_cmd.call_queue(target=tmp_path, tool_id="mcp-runner", args='{"path":"pending"}', json_output=True) == 0
    pending = json.loads(capsys.readouterr().out)["call"]
    assert tools_cmd.call_run(target=tmp_path, call_id=pending["id"], json_output=True) == 1
    assert "must be approved" in "\n".join(json.loads(capsys.readouterr().out)["blockers"])

    rejected = _queue_and_approve_mcp(tmp_path, capsys, args='{"path":"rejected"}')
    assert tools_cmd.call_reject(target=tmp_path, call_id=rejected["id"], reason="no", json_output=True) == 0
    capsys.readouterr()
    assert tools_cmd.call_run(target=tmp_path, call_id=rejected["id"], json_output=True) == 1
    assert "must be approved" in "\n".join(json.loads(capsys.readouterr().out)["blockers"])

    held = _queue_and_approve_mcp(tmp_path, capsys, args='{"path":"held"}')
    assert tools_cmd.call_hold(target=tmp_path, call_id=held["id"], reason="wait", json_output=True) == 0
    capsys.readouterr()
    assert tools_cmd.call_run(target=tmp_path, call_id=held["id"], json_output=True) == 1
    assert "must be approved" in "\n".join(json.loads(capsys.readouterr().out)["blockers"])

    blocked = _queue_and_approve_mcp(tmp_path, capsys, args='{"path":"blocked"}')
    calls = tools_cmd._read_calls(tmp_path)
    for item in calls:
        if item["id"] == blocked["id"]:
            item["blockers"] = ["manual blocker"]
            item["approval_fingerprint"] = tools_cmd._approval_fingerprint(item)
    tools_cmd._write_calls(tmp_path, calls)
    assert tools_cmd.call_run(target=tmp_path, call_id=blocked["id"], json_output=True) == 1
    assert "blocked calls cannot be run" in "\n".join(json.loads(capsys.readouterr().out)["blockers"])

    stale = _queue_and_approve_mcp(tmp_path, capsys, args='{"path":"stale"}')
    (tmp_path / "tools" / "mcp-input.schema.json").write_text(
        json.dumps({"type": "object", "properties": {"path": {"type": "string"}, "mode": {"type": "string"}}})
    )
    assert tools_cmd.call_run(target=tmp_path, call_id=stale["id"], json_output=True) == 1
    assert "contract fingerprint is stale" in "\n".join(json.loads(capsys.readouterr().out)["blockers"])

    _write_mcp_tool_config(tmp_path, server_script=_fake_mcp_server_script())
    missing_runtime = _queue_and_approve_mcp(tmp_path, capsys, args='{"path":"missing-runtime"}')
    assert tools_cmd.call_run(target=tmp_path, call_id=missing_runtime["id"], json_output=True) == 1
    assert "required runtime is not running: helper" in "\n".join(json.loads(capsys.readouterr().out)["blockers"])

    _write_policy_config(tmp_path, allowed_families=["mcp"], allowed_runtimes=["helper"])
    policy_denied = _queue_and_approve_mcp(tmp_path, capsys, args='{"path":"policy"}')
    _write_policy_config(tmp_path, allowed_families=["script"], allowed_runtimes=["helper"])
    assert tools_cmd.call_run(target=tmp_path, call_id=policy_denied["id"], json_output=True) == 1
    assert "family is not allowed by policy: mcp" in "\n".join(json.loads(capsys.readouterr().out)["blockers"])


def test_tools_call_run_mcp_timeout_and_malformed_receipts(tmp_path, capsys):
    _init_git_repo(tmp_path)
    _write_runtime_config(tmp_path)
    _write_policy_config(tmp_path, allowed_families=["mcp"], allowed_runtimes=["helper"])
    assert tools_cmd.runtime_start(target=tmp_path, runtime_id="helper", json_output=True) == 0
    capsys.readouterr()
    try:
        _write_mcp_tool_config(tmp_path, server_script=_fake_mcp_server_script(malformed=True))
        malformed = _queue_and_approve_mcp(tmp_path, capsys, args='{"path":"malformed"}')
        assert tools_cmd.call_run(target=tmp_path, call_id=malformed["id"], json_output=True) == 1
        payload = json.loads(capsys.readouterr().out)
        assert payload["receipt"]["status"] == "failed"
        assert "invalid JSON-RPC response" in payload["receipt"]["stderr_summary"]

        _write_mcp_tool_config(tmp_path, server_script=_fake_mcp_server_script(sleep_seconds=1.0), timeout=0.1)
        timed = _queue_and_approve_mcp(tmp_path, capsys, args='{"path":"timeout"}')
        assert tools_cmd.call_run(target=tmp_path, call_id=timed["id"], json_output=True) == 1
        payload = json.loads(capsys.readouterr().out)
        assert payload["receipt"]["status"] == "failed"
        assert payload["receipt"]["timed_out"] is True
    finally:
        tools_cmd.runtime_stop(target=tmp_path, runtime_id="helper", json_output=True)
        capsys.readouterr()


def test_tools_call_run_mcp_redacts_payloads_and_env_values(tmp_path, monkeypatch, capsys):
    _init_git_repo(tmp_path)
    secret_value = "super-secret-value"
    monkeypatch.setenv("BRIGADE_TEST_SECRET", secret_value)
    _write_mcp_tool_config(tmp_path, server_script=_fake_mcp_server_script(copy_env=True))
    config = tmp_path / ".brigade" / "tools.toml"
    config.write_text(config.read_text() + 'env_labels = ["SAFE_LABEL"]\n')
    _write_runtime_config(tmp_path)
    _write_policy_config(tmp_path, allowed_families=["mcp"], allowed_runtimes=["helper"], env_bindings={"SAFE_LABEL": "BRIGADE_TEST_SECRET"})
    assert tools_cmd.runtime_start(target=tmp_path, runtime_id="helper", json_output=True) == 0
    capsys.readouterr()
    try:
        call = _queue_and_approve_mcp(tmp_path, capsys, args='{"path":"README.md","api_token":"argument-secret"}')
        assert tools_cmd.call_run(target=tmp_path, call_id=call["id"], json_output=True) == 0
        payload = json.loads(capsys.readouterr().out)
        rendered = json.dumps(payload)
        assert secret_value not in rendered
        assert "argument-secret" not in rendered
        assert payload["receipt"]["mcp_request_payload"]["params"]["arguments"]["api_token"] == "[redacted]"
        assert secret_value not in Path(payload["receipt"]["receipt_path"]).read_text()
        assert secret_value not in Path(payload["receipt"]["stdout_log_path"]).read_text()
    finally:
        tools_cmd.runtime_stop(target=tmp_path, runtime_id="helper", json_output=True)
        capsys.readouterr()


def test_tools_call_run_mcp_health_brief_and_imports(tmp_path, monkeypatch, capsys):
    _init_git_repo(tmp_path)
    dogfood_cmd.init(target=tmp_path)
    capsys.readouterr()
    monkeypatch.setattr(work_cmd.shutil, "which", lambda name: f"/usr/bin/{name}")
    monkeypatch.setattr(dogfood_cmd, "_check_git_ignored", lambda repo, path: "yes")
    _write_mcp_tool_config(tmp_path, server_script=_fake_mcp_server_script(malformed=True))
    _write_runtime_config(tmp_path)
    _write_policy_config(tmp_path, allowed_families=["mcp"], allowed_runtimes=["helper"])
    assert tools_cmd.runtime_start(target=tmp_path, runtime_id="helper", json_output=True) == 0
    capsys.readouterr()
    try:
        failed = _queue_and_approve_mcp(tmp_path, capsys)
        assert tools_cmd.call_run(target=tmp_path, call_id=failed["id"], json_output=True) == 1
        run_id = json.loads(capsys.readouterr().out)["receipt"]["id"]
    finally:
        tools_cmd.runtime_stop(target=tmp_path, runtime_id="helper", json_output=True)
        capsys.readouterr()

    assert tools_cmd.doctor(target=tmp_path) == 0
    out = capsys.readouterr().out
    assert "[warn] tool_mcp_execution_failed:" in out

    assert work_cmd.brief(target=tmp_path) == 0
    out = capsys.readouterr().out
    assert "tool_run_top_issue:" in out
    assert run_id in out

    assert tools_cmd.import_issues(target=tmp_path, json_output=True) == 0
    payload = json.loads(capsys.readouterr().out)
    issue_types = {item["metadata"]["tool_issue_type"] for item in payload["imports"]}
    assert "mcp_execution_failed" in issue_types


def test_tools_runtime_init_list_show_status_and_json(tmp_path, capsys):
    _init_git_repo(tmp_path)
    assert tools_cmd.runtime_init(target=tmp_path) == 0
    out = capsys.readouterr().out
    assert "runtime_config:" in out
    assert (tmp_path / ".brigade" / "tools" / "runtimes.toml").is_file()

    assert tools_cmd.runtime_list(target=tmp_path) == 0
    out = capsys.readouterr().out
    assert "tools runtime list:" in out
    assert "local-helper" in out

    assert tools_cmd.runtime_list(target=tmp_path, json_output=True) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["runtime_count"] == 1

    assert tools_cmd.runtime_show(target=tmp_path, runtime_id="local-helper") == 0
    out = capsys.readouterr().out
    assert "runtime: local-helper" in out

    assert tools_cmd.runtime_status(target=tmp_path, json_output=True) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["counts"]["stopped"] == 1


def test_tools_runtime_start_stop_restart_with_pid_logs_and_unmanaged_refusal(tmp_path, capsys):
    _init_git_repo(tmp_path)
    _write_runtime_config(tmp_path)

    assert tools_cmd.runtime_start(target=tmp_path, runtime_id="helper", json_output=True) == 0
    payload = json.loads(capsys.readouterr().out)
    pid = payload["pid"]
    assert payload["runtime"]["state"] == "running"
    assert os.path.isfile(payload["runtime"]["pid_path"])
    assert os.path.isfile(payload["runtime"]["stdout_log_path"])
    assert os.path.isfile(payload["runtime"]["stderr_log_path"])

    assert tools_cmd.runtime_start(target=tmp_path, runtime_id="helper", json_output=True) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["skipped"] == 1
    assert payload["runtime"]["pid"] == pid

    assert tools_cmd.runtime_restart(target=tmp_path, runtime_id="helper", json_output=True) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["runtime"]["state"] == "running"
    assert payload["runtime"]["pid"] != pid

    assert tools_cmd.runtime_stop(target=tmp_path, runtime_id="helper", json_output=True) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["stopped"] == 1
    assert payload["runtime"]["state"] == "stopped"

    process = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
    try:
        pid_path = tmp_path / ".brigade" / "tools" / "runtime" / "helper.pid"
        pid_path.write_text(f"{process.pid}\n")
        assert tools_cmd.runtime_stop(target=tmp_path, runtime_id="helper", json_output=True) == 1
        payload = json.loads(capsys.readouterr().out)
        assert "unmanaged" in payload["error"]
    finally:
        process.terminate()
        process.wait(timeout=5)


def test_tools_runtime_doctor_safety_warnings(tmp_path, capsys):
    _init_git_repo(tmp_path)
    _write_runtime_config(
        tmp_path,
        runtime_id="bad",
        command="bash -c echo hi",
        cwd="missing",
        health_command=f'{sys.executable} -c "import sys; sys.exit(2)"',
    )

    assert tools_cmd.runtime_start(target=tmp_path, runtime_id="bad", json_output=True) == 1
    payload = json.loads(capsys.readouterr().out)
    assert "runtime command shape is high risk" in payload["blockers"]
    assert any("runtime cwd missing" in blocker for blocker in payload["blockers"])

    _write_runtime_config(tmp_path, runtime_id="stale")
    stale_pid = tmp_path / ".brigade" / "tools" / "runtime" / "stale.pid"
    stale_pid.parent.mkdir(parents=True, exist_ok=True)
    stale_pid.write_text("999999\n")
    assert tools_cmd.runtime_doctor(target=tmp_path) == 0
    out = capsys.readouterr().out
    assert "[warn] tool_runtime_stale_pid:" in out

    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind((".".join(("127", "0", "0", "1")), 0))
        sock.listen()
        port = sock.getsockname()[1]
        _write_runtime_config(tmp_path, runtime_id="porty", port=port)
        assert tools_cmd.runtime_doctor(target=tmp_path) == 0
        out = capsys.readouterr().out
        assert "[warn] tool_runtime_port_conflict:" in out

    _write_runtime_config(
        tmp_path,
        runtime_id="health",
        health_command=f'{sys.executable} -c "import sys; sys.exit(3)"',
    )
    assert tools_cmd.runtime_start(target=tmp_path, runtime_id="health", json_output=True) == 0
    capsys.readouterr()
    try:
        assert tools_cmd.runtime_doctor(target=tmp_path) == 0
        out = capsys.readouterr().out
        assert "[warn] tool_runtime_health_failed:" in out
    finally:
        tools_cmd.runtime_stop(target=tmp_path, runtime_id="health", json_output=True)
        capsys.readouterr()


def test_tools_call_run_requires_healthy_runtime_and_receipt_snapshot(tmp_path, capsys):
    _init_git_repo(tmp_path)
    _write_script_tool_config(tmp_path, script='print("ok")\n')
    config = tmp_path / ".brigade" / "tools.toml"
    config.write_text(
        config.read_text()
        + """
runtime_id = "helper"
requires_runtime = true
"""
    )
    _write_runtime_config(tmp_path)
    call = _queue_and_approve_runner(tmp_path, capsys)

    assert tools_cmd.call_run(target=tmp_path, call_id=call["id"], json_output=True) == 1
    payload = json.loads(capsys.readouterr().out)
    assert "required runtime is not running: helper" in payload["blockers"]

    assert tools_cmd.runtime_start(target=tmp_path, runtime_id="helper", json_output=True) == 0
    capsys.readouterr()
    try:
        assert tools_cmd.call_run(target=tmp_path, call_id=call["id"], json_output=True) == 0
        payload = json.loads(capsys.readouterr().out)
        assert payload["receipt"]["runtime_id"] == "helper"
        assert payload["receipt"]["runtime"]["state"] == "running"
        assert payload["receipt"]["runtime"]["managed"] is True
    finally:
        tools_cmd.runtime_stop(target=tmp_path, runtime_id="helper", json_output=True)
        capsys.readouterr()


def test_tools_runtime_health_integrates_with_doctor_brief_and_imports(tmp_path, monkeypatch, capsys):
    _init_git_repo(tmp_path)
    dogfood_cmd.init(target=tmp_path)
    capsys.readouterr()
    monkeypatch.setattr(work_cmd.shutil, "which", lambda name: f"/usr/bin/{name}")
    monkeypatch.setattr(dogfood_cmd, "_check_git_ignored", lambda repo, path: "yes")
    _write_script_tool_config(tmp_path, script='print("ok")\n')
    _write_runtime_config(tmp_path, runtime_id="other")
    config = tmp_path / ".brigade" / "tools.toml"
    config.write_text(
        config.read_text()
        + """
runtime_id = "missing-runtime"
requires_runtime = true
"""
    )

    assert tools_cmd.doctor(target=tmp_path) == 0
    out = capsys.readouterr().out
    assert "[warn] tool_runtime_missing:" in out

    assert work_cmd.brief(target=tmp_path) == 0
    out = capsys.readouterr().out
    assert "tool_top_issue: runner/runtime_missing" in out

    assert tools_cmd.import_issues(target=tmp_path, json_output=True) == 0
    payload = json.loads(capsys.readouterr().out)
    issue_types = {item["metadata"]["tool_issue_type"] for item in payload["imports"]}
    assert "runtime_missing" in issue_types


def test_tools_policy_init_show_doctor_and_json(tmp_path, capsys):
    _init_git_repo(tmp_path)
    assert tools_cmd.policy_init(target=tmp_path) == 0
    out = capsys.readouterr().out
    assert "policy_config:" in out
    assert (tmp_path / ".brigade" / "tools" / "policy.toml").is_file()

    assert tools_cmd.policy_show(target=tmp_path) == 0
    out = capsys.readouterr().out
    assert "tools policy:" in out
    assert "allowed_families: script" in out

    assert tools_cmd.policy_show(target=tmp_path, json_output=True) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["policy"]["allowed_families"] == ["script"]
    assert "SAFE_ENV" in payload["policy"]["env_bindings"]

    assert tools_cmd.policy_doctor(target=tmp_path, json_output=True) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["enabled"] is True
    assert payload["issue_count"] == 0


def test_tools_policy_blocks_plan_and_run_for_effect_timeout_runtime_approval_and_env(tmp_path, capsys):
    _init_git_repo(tmp_path)
    _write_script_tool_config(tmp_path, script='print("ok")\n', timeout=30)
    config = tmp_path / ".brigade" / "tools.toml"
    config.write_text(
        f"""
[[tool]]
id = "runner"
name = "Runner"
family = "script"
enabled = true
description = "Run local script."
command = "{sys.executable} tools/runner.py"
input_schema_path = "tools/input.schema.json"
timeout = 30
permissions = ["read-files"]
effects = ["remote-mutation"]
approval_mode = "never"
argument_template = {{ path = "{{path}}" }}
supported_harnesses = []
runtime_id = "helper"
requires_runtime = false
env_labels = ["SAFE_LABEL"]
"""
    )
    _write_policy_config(
        tmp_path,
        allowed_effects=["local-read"],
        denied_effects=["remote-mutation"],
        required_approval_modes=["on-request"],
        max_timeout=5,
        allowed_runtimes=["other"],
        env_bindings={},
    )

    assert tools_cmd.call_plan(target=tmp_path, tool_id="runner", args='{"path":"README.md"}', json_output=True) == 1
    payload = json.loads(capsys.readouterr().out)
    blockers = "\n".join(payload["blockers"])
    assert "effect is denied by policy: remote-mutation" in blockers
    assert "effect is not allowed by policy: remote-mutation" in blockers
    assert "approval mode is not allowed by policy: never" in blockers
    assert "timeout exceeds policy max: 30.0 > 5.0" in blockers
    assert "runtime is not allowed by policy: helper" in blockers
    assert "missing env binding for label: SAFE_LABEL" in blockers

    assert tools_cmd.call_queue(target=tmp_path, tool_id="runner", args='{"path":"README.md"}', include_blocked=True, json_output=True) == 0
    call = json.loads(capsys.readouterr().out)["call"]
    calls = tools_cmd._read_calls(tmp_path)
    calls[0]["status"] = "approved"
    calls[0]["reviewed_at"] = "2026-05-27T12:00:00+00:00"
    calls[0]["approval_fingerprint"] = tools_cmd._approval_fingerprint(calls[0])
    tools_cmd._write_calls(tmp_path, calls)
    assert tools_cmd.call_run(target=tmp_path, call_id=call["id"], json_output=True) == 1
    payload = json.loads(capsys.readouterr().out)
    assert "effect is denied by policy: remote-mutation" in "\n".join(payload["blockers"])


def test_tools_policy_env_binding_passes_values_without_storing_them(tmp_path, monkeypatch, capsys):
    _init_git_repo(tmp_path)
    secret_value = "super-secret-value"
    monkeypatch.setenv("BRIGADE_TEST_SECRET", secret_value)
    _write_script_tool_config(
        tmp_path,
        script='import os\nprint("label=" + os.environ.get("SAFE_LABEL", ""))\n',
    )
    config = tmp_path / ".brigade" / "tools.toml"
    config.write_text(
        config.read_text()
        + """
env_labels = ["SAFE_TOKEN"]
"""
    )
    config.write_text(config.read_text().replace('env_labels = ["SAFE_TOKEN"]', 'env_labels = ["SAFE_LABEL"]'))
    _write_policy_config(tmp_path, env_bindings={"SAFE_LABEL": "BRIGADE_TEST_SECRET"})

    assert tools_cmd.call_plan(target=tmp_path, tool_id="runner", args='{"path":"README.md"}', json_output=True) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["policy"]["env_labels_used"] == ["SAFE_LABEL"]
    assert secret_value not in json.dumps(payload)

    call = _queue_and_approve_runner(tmp_path, capsys)
    assert secret_value not in (tmp_path / ".brigade" / "tools" / "calls.jsonl").read_text()

    assert tools_cmd.call_run(target=tmp_path, call_id=call["id"], json_output=True) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["receipt"]["env_labels_used"] == ["SAFE_LABEL"]
    assert payload["receipt"]["policy"]["env_labels_used"] == ["SAFE_LABEL"]
    assert secret_value not in json.dumps(payload)
    assert secret_value not in (tmp_path / ".brigade" / "tools" / "runs" / f"{payload['receipt']['id']}.stdout.log").read_text()
    assert "[redacted]" in (tmp_path / ".brigade" / "tools" / "runs" / f"{payload['receipt']['id']}.stdout.log").read_text()
    assert secret_value not in Path(payload["receipt"]["receipt_path"]).read_text()


def test_tools_policy_health_integrates_with_doctor_brief_and_imports(tmp_path, monkeypatch, capsys):
    _init_git_repo(tmp_path)
    dogfood_cmd.init(target=tmp_path)
    capsys.readouterr()
    monkeypatch.setattr(work_cmd.shutil, "which", lambda name: f"/usr/bin/{name}")
    monkeypatch.setattr(dogfood_cmd, "_check_git_ignored", lambda repo, path: "yes")
    _write_script_tool_config(tmp_path, script='print("ok")\n')
    _write_policy_config(tmp_path, denied_effects=["local-read"])

    assert tools_cmd.doctor(target=tmp_path) == 0
    out = capsys.readouterr().out
    assert "[warn] tool_policy_denied_effect:" in out

    assert work_cmd.brief(target=tmp_path) == 0
    out = capsys.readouterr().out
    assert "tool_top_issue: runner/policy_denied_effect" in out

    assert tools_cmd.import_issues(target=tmp_path, json_output=True) == 0
    payload = json.loads(capsys.readouterr().out)
    issue_types = {item["metadata"]["tool_issue_type"] for item in payload["imports"]}
    assert "policy_denied_effect" in issue_types


def test_work_backup_init_status_doctor_and_json(tmp_path, monkeypatch, capsys):
    _init_git_repo(tmp_path)
    monkeypatch.setattr(dogfood_cmd, "_check_git_ignored", lambda repo, path: "yes")
    now = datetime(2026, 5, 30, 12, 0, 0, tzinfo=timezone.utc)
    monkeypatch.setattr(work_cmd, "_now", lambda: now)

    assert work_cmd.backup_init(target=tmp_path) == 0
    out = capsys.readouterr().out
    config = tmp_path / ".brigade" / "backups.toml"
    assert f"backup_config: {config}" in out
    assert "destinations: 2" in out
    assert ".brigade/backups.toml" in (tmp_path / ".gitignore").read_text()

    nas = tmp_path / ".brigade" / "backups" / "nas-summary.json"
    cloud = tmp_path / ".brigade" / "backups" / "cloud-summary.json"
    nas.parent.mkdir(parents=True)
    for path, label in ((nas, "NAS backup"), (cloud, "Cloud backup")):
        _write_json(
            path,
            {
                "destination_label": label,
                "latest_snapshot_at": "2026-05-30T06:00:00+00:00",
                "latest_check_at": "2026-05-29T12:00:00+00:00",
                "latest_check_result": "ok",
                "latest_prune_at": "2026-05-29T12:30:00+00:00",
                "latest_prune_result": "ok",
                "latest_restore_rehearsal_at": "2026-05-01T12:00:00+00:00",
                "latest_restore_rehearsal_result": "ok",
                "summary": f"{label} is current.",
                "evidence_path": f".brigade/backups/{path.name}",
            },
        )

    assert work_cmd.backup_status(target=tmp_path) == 0
    out = capsys.readouterr().out
    assert "work backup status:" in out
    assert "- nas [enabled] nas issues=0" in out
    assert "- cloud [enabled] cloud issues=0" in out
    assert "top_issue: none" in out

    assert work_cmd.backup_status(target=tmp_path, json_output=True) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["valid"] is True
    assert payload["issue_count"] == 0

    assert work_cmd.backup_doctor(target=tmp_path) == 0
    out = capsys.readouterr().out
    assert "[ok] backup_config:" in out
    assert "backup_issues: 0" in out


def test_work_backup_doctor_warns_for_backup_health_issues(tmp_path, monkeypatch, capsys):
    _init_git_repo(tmp_path)
    monkeypatch.setattr(
        work_cmd,
        "_now",
        lambda: datetime(2026, 5, 30, 12, 0, 0, tzinfo=timezone.utc),
    )
    config = tmp_path / ".brigade" / "backups.toml"
    config.parent.mkdir(parents=True, exist_ok=True)
    config.write_text(
        """
[[destination]]
id = "nas"
kind = "nas"
command_label = "safe summary"
summary_path = ".brigade/backups/nas-summary.json"
snapshot_stale_hours = 24
check_stale_hours = 48
prune_stale_hours = 48
restore_rehearsal_stale_days = 30
enabled = true

[[destination]]
id = "cloud"
kind = "cloud"
command_label = "safe summary"
summary_path = ".brigade/backups/cloud-summary.json"
snapshot_stale_hours = 24
check_stale_hours = 48
prune_stale_hours = 48
restore_rehearsal_stale_days = 30
enabled = true
"""
    )
    nas = tmp_path / ".brigade" / "backups" / "nas-summary.json"
    nas.parent.mkdir(parents=True)
    _write_json(
        nas,
        {
            "destination_label": "NAS backup",
            "latest_snapshot_at": "2026-05-25T12:00:00+00:00",
            "latest_check_at": "2026-05-30T10:00:00+00:00",
            "latest_check_result": "failed",
            "latest_prune_at": "2026-05-20T12:00:00+00:00",
            "latest_prune_result": "ok",
            "latest_restore_rehearsal_at": "2026-04-01T12:00:00+00:00",
            "latest_restore_rehearsal_result": "ok",
            "summary": "NAS backup has stale evidence.",
            "evidence_path": ".brigade/backups/nas-evidence.json",
            "hostname": "private-host",
            "repo_path": "/private/restic/repo",
            "webhook_url": "https://example.invalid/hook",
        },
    )

    assert work_cmd.backup_doctor(target=tmp_path) == 0
    out = capsys.readouterr().out
    assert "[warn] backup_missing_summary: missing summary:" in out
    assert "[warn] backup_unsafe_summary_fields:" in out
    assert "hostname" in out
    assert "private-host" not in out
    assert "[warn] backup_snapshot_stale: NAS backup latest snapshot is 120.0h old" in out
    assert "[warn] backup_check_failed: NAS backup latest check result is failed" in out
    assert "[warn] backup_prune_stale: NAS backup latest prune is 240.0h old" in out
    assert "[warn] backup_restore_rehearsal_overdue: NAS backup latest restore rehearsal is 59.0d old" in out

    assert work_cmd.backup_doctor(target=tmp_path, json_output=True) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["issue_count"] >= 6
    rendered = json.dumps(payload, sort_keys=True)
    assert "private-host" not in rendered
    assert "repo_path" in rendered


def test_work_backup_import_issues_dedupes_and_respects_dismissed_until_change(tmp_path, monkeypatch, capsys):
    _init_git_repo(tmp_path)
    monkeypatch.setattr(
        work_cmd,
        "_now",
        lambda: datetime(2026, 5, 30, 12, 0, 0, tzinfo=timezone.utc),
    )
    config = tmp_path / ".brigade" / "backups.toml"
    config.parent.mkdir(parents=True)
    config.write_text(
        """
[[destination]]
id = "nas"
kind = "nas"
command_label = "safe summary"
summary_path = ".brigade/backups/nas-summary.json"
snapshot_stale_hours = 24
check_stale_hours = 48
prune_stale_hours = 48
restore_rehearsal_stale_days = 30
enabled = true
"""
    )
    summary = tmp_path / ".brigade" / "backups" / "nas-summary.json"
    summary.parent.mkdir(parents=True)
    _write_json(
        summary,
        {
            "destination_label": "NAS backup",
            "latest_snapshot_at": "2026-05-25T12:00:00+00:00",
            "latest_check_at": "2026-05-30T10:00:00+00:00",
            "latest_check_result": "ok",
            "latest_prune_at": "2026-05-30T10:00:00+00:00",
            "latest_prune_result": "ok",
            "latest_restore_rehearsal_at": "2026-05-01T12:00:00+00:00",
            "latest_restore_rehearsal_result": "ok",
            "summary": "NAS backup snapshot is stale.",
            "evidence_path": ".brigade/backups/nas-evidence.json",
        },
    )

    assert work_cmd.backup_import_issues(target=tmp_path, json_output=True) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["created"] == 1
    item = payload["imports"][0]
    assert item["source"] == "backup-health"
    assert item["metadata"]["backup_destination"] == "nas"
    assert item["metadata"]["backup_issue_type"] == "snapshot_stale"

    assert work_cmd.backup_import_issues(target=tmp_path, json_output=True) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["created"] == 0
    assert payload["skipped"] == 1

    assert work_cmd.import_dismiss(target=tmp_path, import_id=item["id"], reason="ack") == 0
    capsys.readouterr()
    assert work_cmd.backup_import_issues(target=tmp_path, json_output=True) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["created"] == 0
    assert payload["dismissed"] == 1

    data = json.loads(summary.read_text())
    data["latest_snapshot_at"] = "2026-05-24T12:00:00+00:00"
    _write_json(summary, data)
    assert work_cmd.backup_import_issues(target=tmp_path, json_output=True) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["created"] == 1


def test_work_brief_and_doctor_include_backup_health(tmp_path, monkeypatch, capsys):
    _init_git_repo(tmp_path)
    dogfood_cmd.init(target=tmp_path)
    monkeypatch.setattr(work_cmd.shutil, "which", lambda name: f"/usr/bin/{name}")
    monkeypatch.setattr(dogfood_cmd, "_check_git_ignored", lambda repo, path: "yes")
    monkeypatch.setattr(
        work_cmd,
        "_now",
        lambda: datetime(2026, 5, 30, 12, 0, 0, tzinfo=timezone.utc),
    )
    assert work_cmd.backup_init(target=tmp_path, update_gitignore=False) == 0
    capsys.readouterr()

    assert work_cmd.brief(target=tmp_path) == 0
    out = capsys.readouterr().out
    assert "backup_config:" in out
    assert "backup_health:" in out
    assert "backup_top_issue:" in out

    assert work_cmd.doctor(target=tmp_path) == 0
    out = capsys.readouterr().out
    assert "[ok] backup_config:" in out
    assert "[warn] backup_missing_summary:" in out


def test_work_scanners_plan_detects_conflicts_and_suggests_staggering(tmp_path, capsys):
    _init_git_repo(tmp_path)
    config = tmp_path / ".brigade" / "scanners.toml"
    config.parent.mkdir(parents=True)
    config.write_text(
        """
[[scanner]]
id = "chat-memory-sweep"
source = "chat-memory-sweep"
command = "brigade work import chat-sweep --json"
cadence = "daily@02:00"
enabled = true
timeout = 900
output_path = ".brigade/chat-memory-sweeps/latest.json"
conflict_window = "02:00-02:30"

[[scanner]]
id = "memory-refresh"
source = "memory-refresh"
command = "brigade work import memory-refresh --json"
cadence = "daily@02:05"
enabled = true
timeout = 300
output_path = "memory/cards/decay/refresh-queue.json"
conflict_window = "02:10-02:40"
"""
    )

    assert work_cmd.scanners_plan(target=tmp_path) == 0
    out = capsys.readouterr().out
    assert "run_overlap: chat-memory-sweep, memory-refresh" in out
    assert "window_overlap: chat-memory-sweep, memory-refresh" in out
    assert "clustered_runs: chat-memory-sweep, memory-refresh" in out
    assert "memory-refresh: daily@02:15" in out

    assert work_cmd.scanners_plan(target=tmp_path, json_output=True) == 0
    payload = json.loads(capsys.readouterr().out)
    assert {item["type"] for item in payload["conflicts"]} == {
        "run_overlap",
        "window_overlap",
        "clustered_runs",
    }
    assert payload["suggestions"][1]["suggested_cadence"] == "daily@02:15"


def test_work_scanners_run_writes_receipt_and_reports_import_counts(tmp_path, capsys):
    _init_git_repo(tmp_path)
    script = tmp_path / "scanner.py"
    script.write_text(
        """
import json
from pathlib import Path

root = Path.cwd()
output = root / ".brigade" / "scanner-output.json"
output.parent.mkdir(parents=True, exist_ok=True)
output.write_text(json.dumps({"ok": True}) + "\\n")
inbox = root / ".brigade" / "work" / "imports" / "inbox.jsonl"
inbox.parent.mkdir(parents=True, exist_ok=True)
record = {
    "id": "scan-import-1",
    "kind": "task",
    "source": "repo-scan",
    "text": "Review scanner output",
    "status": "pending",
    "created_at": "2026-05-28T12:00:00+00:00",
    "updated_at": "2026-05-28T12:00:00+00:00",
}
inbox.write_text(json.dumps(record) + "\\n")
print("scanner complete")
"""
    )
    config = tmp_path / ".brigade" / "scanners.toml"
    config.parent.mkdir(parents=True, exist_ok=True)
    config.write_text(
        f"""
[[scanner]]
id = "repo-scan"
source = "repo-scan"
command = "{sys.executable} {script}"
cadence = "daily@02:00"
enabled = true
timeout = 30
output_path = ".brigade/scanner-output.json"
conflict_window = "02:00-02:10"
"""
    )

    assert work_cmd.scanners_run(target=tmp_path, scanner_id="repo-scan") == 0
    out = capsys.readouterr().out
    assert "work scanners run:" in out
    assert "completed: 1" in out
    assert "pending_imports_before: 0" in out
    assert "pending_imports_after: 1" in out

    receipts = list((tmp_path / ".brigade" / "scanners" / "runs").glob("*/receipt.json"))
    assert len(receipts) == 1
    receipt = json.loads(receipts[0].read_text())
    assert receipt["scanner_id"] == "repo-scan"
    assert receipt["status"] == "completed"
    assert receipt["exit_code"] == 0
    assert receipt["timed_out"] is False
    assert receipt["stdout_summary"] == "scanner complete"
    assert Path(receipt["stdout_path"]).is_file()
    assert Path(receipt["stderr_path"]).is_file()
    assert receipt["output_before"] == {"path": str(tmp_path / ".brigade" / "scanner-output.json"), "exists": False}
    assert receipt["output_after"]["exists"] is True
    assert receipt["provenance_imports_stamped"] == 1
    imports = json.loads((tmp_path / ".brigade" / "work" / "imports" / "inbox.jsonl").read_text().splitlines()[0])
    assert imports["metadata"]["scanner_run_id"] == receipt["run_id"]
    assert imports["metadata"]["scanner_id"] == "repo-scan"
    assert imports["metadata"]["source_fingerprint"]

    assert work_cmd.scanners_run(target=tmp_path, scanner_id="repo-scan", json_output=True) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["completed"] == 1
    assert payload["imports_after"]["by_source"] == {"repo-scan": 1}
    assert payload["runs"][0]["provenance_imports_stamped"] == 0


def test_work_scanners_run_ingest_output_adds_provenance_only_with_flag(tmp_path, capsys):
    _init_git_repo(tmp_path)
    script = tmp_path / "scanner.py"
    script.write_text(
        """
import json
from pathlib import Path

root = Path.cwd()
path = root / ".brigade" / "scanner-imports.jsonl"
path.parent.mkdir(parents=True, exist_ok=True)
record = {
    "kind": "task",
    "source": "repo-scan",
    "text": "Review generated finding",
    "metadata": {"source_item_key": "finding-1"},
    "acceptance": ["Finding is reviewed."],
}
path.write_text(json.dumps(record) + "\\n")
print("wrote imports")
"""
    )
    config = tmp_path / ".brigade" / "scanners.toml"
    config.parent.mkdir(parents=True, exist_ok=True)
    config.write_text(
        f"""
[[scanner]]
id = "repo-scan"
source = "repo-scan"
command = "{sys.executable} {script}"
cadence = "daily@02:00"
enabled = true
timeout = 30
output_path = ".brigade/scanner-imports.jsonl"
import_path = ".brigade/scanner-imports.jsonl"
import_format = "jsonl"
conflict_window = "02:00-02:10"
"""
    )

    assert work_cmd.scanners_run(target=tmp_path, scanner_id="repo-scan") == 0
    out = capsys.readouterr().out
    assert "pending_imports_after: 0" in out
    assert work_cmd.import_list(target=tmp_path, json_output=True) == 0
    assert json.loads(capsys.readouterr().out)["imports"] == []

    assert work_cmd.scanners_run(target=tmp_path, scanner_id="repo-scan", ingest_output=True, json_output=True) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["ingest_output"] is True
    assert payload["runs"][0]["ingest_output"]["created"] == 1
    assert payload["imports_after"]["by_source"] == {"repo-scan": 1}

    item = payload["runs"][0]
    assert work_cmd.import_list(target=tmp_path, json_output=True) == 0
    imports = json.loads(capsys.readouterr().out)["imports"]
    assert len(imports) == 1
    metadata = imports[0]["metadata"]
    assert metadata["scanner_id"] == "repo-scan"
    assert metadata["scanner_source"] == "repo-scan"
    assert metadata["scanner_run_id"] == item["run_id"]
    assert metadata["scanner_receipt_path"].endswith("/receipt.json")
    assert metadata["scanner_import_path"].endswith(".brigade/scanner-imports.jsonl")
    assert metadata["source_fingerprint"]
    assert metadata["scanner_output_path_snapshot"]["exists"] is True


def test_work_scanners_run_ingest_output_rejects_malformed_without_partial_write(tmp_path, capsys):
    _init_git_repo(tmp_path)
    script = tmp_path / "scanner.py"
    script.write_text(
        """
from pathlib import Path

path = Path.cwd() / ".brigade" / "bad-imports.jsonl"
path.parent.mkdir(parents=True, exist_ok=True)
path.write_text("{not json\\n")
print("wrote bad imports")
"""
    )
    config = tmp_path / ".brigade" / "scanners.toml"
    config.parent.mkdir(parents=True)
    config.write_text(
        f"""
[[scanner]]
id = "repo-scan"
source = "repo-scan"
command = "{sys.executable} {script}"
cadence = "daily@02:00"
enabled = true
timeout = 30
output_path = ".brigade/bad-imports.jsonl"
import_path = ".brigade/bad-imports.jsonl"
import_format = "jsonl"
conflict_window = "02:00-02:10"
"""
    )

    assert work_cmd.scanners_run(target=tmp_path, scanner_id="repo-scan", ingest_output=True, json_output=True) == 2
    payload = json.loads(capsys.readouterr().out)
    assert payload["ingest_errors"]
    assert payload["imports_after"]["total"] == 0
    assert not (tmp_path / ".brigade" / "work" / "imports" / "inbox.jsonl").exists()


def test_work_scanners_due_all_disabled_and_receipt_review(tmp_path, capsys):
    _init_git_repo(tmp_path)
    script = tmp_path / "scanner.py"
    script.write_text("print('ok')\n")
    config = tmp_path / ".brigade" / "scanners.toml"
    config.parent.mkdir(parents=True)
    config.write_text(
        f"""
[[scanner]]
id = "enabled-scan"
source = "enabled-scan"
command = "{sys.executable} {script}"
cadence = "daily@02:00"
enabled = true
timeout = 30
output_path = ".brigade/enabled.json"
conflict_window = "02:00-02:10"

[[scanner]]
id = "disabled-scan"
source = "disabled-scan"
command = "{sys.executable} {script}"
cadence = "daily@03:00"
enabled = false
timeout = 30
output_path = ".brigade/disabled.json"
conflict_window = "03:00-03:10"
"""
    )

    assert work_cmd.scanners_run(target=tmp_path, due=True, json_output=True) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["completed"] == 1
    assert payload["runs"][0]["scanner_id"] == "enabled-scan"
    assert payload["skipped"] == [{"reason": "disabled", "scanner_id": "disabled-scan"}]

    assert work_cmd.scanners_run(target=tmp_path, scanner_id="disabled-scan") == 2
    assert "scanner disabled: disabled-scan" in capsys.readouterr().err

    assert work_cmd.scanners_run(target=tmp_path, due=True, json_output=True) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["selected"] == 0
    assert sorted(item["reason"] for item in payload["skipped"]) == ["disabled", "not_due"]

    assert work_cmd.scanners_run(target=tmp_path, all_matching=True, include_disabled=True, json_output=True) == 0
    payload = json.loads(capsys.readouterr().out)
    assert {item["scanner_id"] for item in payload["runs"]} == {"enabled-scan", "disabled-scan"}

    assert work_cmd.scanners_runs(target=tmp_path, json_output=True) == 0
    runs_payload = json.loads(capsys.readouterr().out)
    assert len(runs_payload["runs"]) == 3
    run_id = runs_payload["runs"][0]["run_id"]

    assert work_cmd.scanners_run_show(target=tmp_path, run_id=run_id) == 0
    out = capsys.readouterr().out
    assert f"scanner_run: {run_id}" in out
    assert "status: completed" in out


def test_work_scanners_run_refuses_risky_running_timeout_and_failure(tmp_path, capsys):
    _init_git_repo(tmp_path)
    script = tmp_path / "scanner.py"
    script.write_text(
        """
import sys
import time

if sys.argv[1] == "timeout":
    time.sleep(1)
elif sys.argv[1] == "fail":
    print("bad output")
    print("bad error", file=sys.stderr)
    raise SystemExit(7)
"""
    )
    config = tmp_path / ".brigade" / "scanners.toml"
    config.parent.mkdir(parents=True)
    config.write_text(
        f"""
[[scanner]]
id = "risky-scan"
source = "risky-scan"
command = "bash -lc echo"
cadence = "daily@02:00"
enabled = true
timeout = 30
output_path = ".brigade/risky.json"
conflict_window = "02:00-02:10"

[[scanner]]
id = "timeout-scan"
source = "timeout-scan"
command = "{sys.executable} {script} timeout"
cadence = "daily@03:00"
enabled = true
timeout = 0.01
output_path = ".brigade/timeout.json"
conflict_window = "03:00-03:10"

[[scanner]]
id = "fail-scan"
source = "fail-scan"
command = "{sys.executable} {script} fail"
cadence = "daily@04:00"
enabled = true
timeout = 30
output_path = ".brigade/fail.json"
conflict_window = "04:00-04:10"
"""
    )
    running = tmp_path / ".brigade" / "scanners" / "runs" / "running"
    running.mkdir(parents=True)
    _write_json(
        running / "receipt.json",
        {
            "run_id": "running",
            "scanner_id": "other",
            "status": "running",
            "started_at": "2026-05-28T12:00:00+00:00",
        },
    )

    assert work_cmd.scanners_run(target=tmp_path, scanner_id="risky-scan") == 2
    assert "scanner run already in progress" in capsys.readouterr().err

    assert work_cmd.scanners_run(target=tmp_path, scanner_id="risky-scan", force=True) == 1
    out = capsys.readouterr().out
    assert "high-risk scanner command: bash" in out

    assert work_cmd.scanners_run(target=tmp_path, scanner_id="timeout-scan", force=True, json_output=True) == 1
    timeout_payload = json.loads(capsys.readouterr().out)
    assert timeout_payload["runs"][0]["timed_out"] is True
    assert "timed out" in timeout_payload["runs"][0]["error"]

    assert work_cmd.scanners_run(target=tmp_path, scanner_id="fail-scan", force=True, json_output=True) == 1
    fail_payload = json.loads(capsys.readouterr().out)
    assert fail_payload["runs"][0]["exit_code"] == 7
    assert fail_payload["runs"][0]["stdout_summary"] == "bad output"
    assert fail_payload["runs"][0]["stderr_summary"] == "bad error"


def test_work_scanners_execution_health_surfaces_and_imports_issues(tmp_path, monkeypatch, capsys):
    _init_git_repo(tmp_path)
    monkeypatch.setattr(work_cmd.shutil, "which", lambda name: f"/usr/bin/{name}")
    monkeypatch.setattr(
        work_cmd,
        "_now",
        lambda: datetime(2026, 5, 30, 12, 0, 0, tzinfo=timezone.utc),
    )
    config = tmp_path / ".brigade" / "scanners.toml"
    config.parent.mkdir(parents=True)
    config.write_text(
        """
[[scanner]]
id = "due-scan"
source = "due-scan"
command = "python3 scanner.py"
cadence = "daily@02:00"
enabled = true
timeout = 30
output_path = ".brigade/due.json"
conflict_window = "02:00-02:10"
"""
    )
    run_dir = tmp_path / ".brigade" / "scanners" / "runs" / "failed-run"
    run_dir.mkdir(parents=True)
    _write_json(
        run_dir / "receipt.json",
        {
            "run_id": "failed-run",
            "scanner_id": "due-scan",
            "source": "due-scan",
            "status": "failed",
            "started_at": "2026-05-29T12:00:00+00:00",
            "completed_at": "2026-05-29T12:00:01+00:00",
            "exit_code": 2,
            "timed_out": False,
            "stdout_path": str(run_dir / "missing-stdout.log"),
            "stderr_path": str(run_dir / "missing-stderr.log"),
        },
    )
    success_dir = tmp_path / ".brigade" / "scanners" / "runs" / "old-success"
    success_dir.mkdir(parents=True)
    (success_dir / "stdout.log").write_text("ok\n")
    (success_dir / "stderr.log").write_text("")
    _write_json(
        success_dir / "receipt.json",
        {
            "run_id": "old-success",
            "scanner_id": "due-scan",
            "source": "due-scan",
            "status": "completed",
            "started_at": "2026-05-25T12:00:00+00:00",
            "completed_at": "2026-05-25T12:00:01+00:00",
            "exit_code": 0,
            "timed_out": False,
            "stdout_path": str(success_dir / "stdout.log"),
            "stderr_path": str(success_dir / "stderr.log"),
        },
    )
    bad_run = tmp_path / ".brigade" / "scanners" / "runs" / "bad-run"
    bad_run.mkdir(parents=True)
    (bad_run / "receipt.json").write_text("{not json\n")

    assert work_cmd.scanners_doctor(target=tmp_path, import_issues=True) == 1
    out = capsys.readouterr().out
    assert "[fail] scanner_run_receipts: bad-run" in out
    assert "[warn] scanner_runs_failed: due-scan:failed-run" in out
    assert "[warn] scanner_run_logs: failed-run:stdout_path" in out
    assert "[warn] scanner_runs_stale: due-scan=120.0h" in out
    assert "[warn] scanner_runs_due: due-scan" in out
    assert "imported_issues:" in out

    assert work_cmd.import_list(target=tmp_path, json_output=True) == 0
    imports = json.loads(capsys.readouterr().out)["imports"]
    assert any(item["source"] == "scanner-health" for item in imports)

    assert work_cmd.brief(target=tmp_path) == 0
    out = capsys.readouterr().out
    assert "scanner_latest_run: due-scan [failed] failed-run" in out
    assert "scanner_due: due-scan" in out

    assert work_cmd.doctor(target=tmp_path) == 1
    out = capsys.readouterr().out
    assert "[warn] scanner_runs_failed:" in out


def test_work_inbox_doctor_reports_hygiene_issues_and_daily_loop(tmp_path, monkeypatch, capsys):
    _init_git_repo(tmp_path)
    dogfood_cmd.init(target=tmp_path)
    monkeypatch.setattr(work_cmd.shutil, "which", lambda name: f"/usr/bin/{name}")
    monkeypatch.setattr(
        work_cmd,
        "_now",
        lambda: datetime(2026, 5, 30, 12, 0, 0, tzinfo=timezone.utc),
    )
    config = tmp_path / ".brigade" / "scanners.toml"
    config.parent.mkdir(parents=True, exist_ok=True)
    config.write_text(
        """
[[scanner]]
id = "repo-scan"
source = "repo-scan"
command = "python3 scanner.py"
cadence = "daily@02:00"
enabled = true
timeout = 30
output_path = ".brigade/repo-scan.jsonl"
import_path = ".brigade/repo-scan.jsonl"
import_format = "jsonl"
conflict_window = "02:00-02:10"
"""
    )
    missing = work_cmd._make_import("Missing provenance", kind="task", source="repo-scan")
    missing["created_at"] = "2026-05-30T11:00:00+00:00"
    stale = work_cmd._make_import("Stale pending", kind="task", source="manual")
    stale["created_at"] = "2026-05-20T12:00:00+00:00"
    promoted = work_cmd._make_import("Broken promoted", kind="task", source="repo-scan")
    promoted.update({"status": "promoted", "task_id": "missing-task", "updated_at": "2026-05-29T12:00:00+00:00"})
    dismissed_changed = work_cmd._make_import(
        "Dismissed old",
        kind="task",
        source="repo-scan",
        metadata={"source_item_key": "same-item", "source_fingerprint": "old"},
    )
    dismissed_changed.update({"status": "dismissed", "dismissed_at": "2026-05-29T12:00:00+00:00"})
    changed_pending = work_cmd._make_import(
        "Dismissed new",
        kind="task",
        source="repo-scan",
        metadata={
            "source_item_key": "same-item",
            "source_fingerprint": "new",
            "scanner_id": "repo-scan",
            "scanner_source": "repo-scan",
        },
    )
    noisy = []
    for index in range(work_cmd.DISMISSED_SOURCE_WARN_THRESHOLD):
        item = work_cmd._make_import(f"Noisy {index}", kind="task", source="noisy-source")
        item.update({"status": "dismissed", "dismissed_at": "2026-05-29T12:00:00+00:00"})
        noisy.append(item)
    work_cmd._write_imports(tmp_path, [missing, stale, promoted, dismissed_changed, changed_pending, *noisy])
    run_dir = tmp_path / ".brigade" / "scanners" / "runs" / "no-import-run"
    run_dir.mkdir(parents=True)
    _write_json(
        run_dir / "receipt.json",
        {
            "run_id": "no-import-run",
            "scanner_id": "repo-scan",
            "source": "repo-scan",
            "status": "completed",
            "started_at": "2026-05-30T10:00:00+00:00",
            "completed_at": "2026-05-30T10:00:01+00:00",
            "exit_code": 0,
            "timed_out": False,
            "import_path": str(tmp_path / ".brigade" / "repo-scan.jsonl"),
        },
    )

    assert work_cmd.inbox_doctor(target=tmp_path) == 0
    out = capsys.readouterr().out
    assert "[warn] inbox_missing_provenance:" in out
    assert "[warn] inbox_stale_pending:" in out
    assert "[warn] inbox_promoted_task_missing:" in out
    assert "[warn] inbox_dismissed_changed:" in out
    assert "[warn] inbox_noisy_sources:" in out
    assert "[warn] inbox_scanner_run_no_imports:" in out

    assert work_cmd.inbox_doctor(target=tmp_path, json_output=True) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["issue_count"] == 6
    assert payload["top_issue"]["name"] == "inbox_missing_provenance"

    assert work_cmd.brief(target=tmp_path) == 0
    out = capsys.readouterr().out
    assert "inbox_hygiene: 6 issue(s)" in out
    assert "inbox_top_issue: inbox_missing_provenance" in out

    assert work_cmd.doctor(target=tmp_path) == 0
    out = capsys.readouterr().out
    assert "[warn] inbox_missing_provenance:" in out
    assert "[warn] inbox_scanner_run_no_imports:" in out


def test_work_inbox_archive_preserves_pending_and_archives_closed(tmp_path, monkeypatch, capsys):
    _init_git_repo(tmp_path)
    monkeypatch.setattr(
        work_cmd,
        "_now",
        lambda: datetime(2026, 5, 30, 12, 0, 0, tzinfo=timezone.utc),
    )
    pending = work_cmd._make_import("Keep pending", kind="task", source="repo-scan")
    pending.update({"status": "pending", "updated_at": "2026-05-20T12:00:00+00:00"})
    promoted = work_cmd._make_import("Archive promoted", kind="task", source="repo-scan")
    promoted.update({"status": "promoted", "updated_at": "2026-05-20T12:00:00+00:00"})
    dismissed = work_cmd._make_import("Archive dismissed", kind="task", source="repo-scan")
    dismissed.update({"status": "dismissed", "updated_at": "2026-05-20T12:00:00+00:00"})
    superseded = work_cmd._make_import("Archive superseded", kind="task", source="repo-scan")
    superseded.update({"status": "superseded", "updated_at": "2026-05-20T12:00:00+00:00"})
    fresh = work_cmd._make_import("Keep fresh dismissed", kind="task", source="repo-scan")
    fresh.update({"status": "dismissed", "updated_at": "2026-05-30T11:00:00+00:00"})
    work_cmd._write_imports(tmp_path, [pending, promoted, dismissed, superseded, fresh])

    assert work_cmd.inbox_archive(target=tmp_path, json_output=True) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["archived"] == 3
    assert payload["kept"] == 2

    remaining = [item["text"] for item in work_cmd._read_imports(tmp_path)]
    assert remaining == ["Keep pending", "Keep fresh dismissed"]
    archived = [
        json.loads(line)
        for line in (tmp_path / ".brigade" / "work" / "imports" / "archive.jsonl").read_text().splitlines()
    ]
    assert [item["text"] for item in archived] == ["Archive promoted", "Archive dismissed", "Archive superseded"]
    assert all(item["archived_at"] == "2026-05-30T12:00:00+00:00" for item in archived)


def test_work_sweep_runs_due_scanners_ingests_output_and_reports(tmp_path, capsys):
    _init_git_repo(tmp_path)
    script = tmp_path / "scanner.py"
    script.write_text(
        """
import json
from pathlib import Path

path = Path.cwd() / ".brigade" / "scanner-imports.jsonl"
path.parent.mkdir(parents=True, exist_ok=True)
record = {
    "kind": "task",
    "source": "repo-scan",
    "text": "Review sweep finding",
    "metadata": {"source_item_key": "finding-1"},
    "acceptance": ["Sweep finding is reviewed."],
}
path.write_text(json.dumps(record) + "\\n")
print("sweep scanner complete")
"""
    )
    config = tmp_path / ".brigade" / "scanners.toml"
    config.parent.mkdir(parents=True)
    config.write_text(
        f"""
[[scanner]]
id = "repo-scan"
source = "repo-scan"
command = "{sys.executable} {script}"
cadence = "daily@02:00"
enabled = true
timeout = 30
output_path = ".brigade/scanner-imports.jsonl"
import_path = ".brigade/scanner-imports.jsonl"
import_format = "jsonl"
conflict_window = "02:00-02:10"
"""
    )

    assert work_cmd.sweep(target=tmp_path, json_output=True) == 0
    report = json.loads(capsys.readouterr().out)
    assert report["status"] == "completed"
    assert report["mode"] == "due"
    assert report["scanner_run_ids"]
    assert report["receipt_paths"][0].endswith("/receipt.json")
    assert report["import_counts"] == {"created": 1, "dismissed": 0, "skipped": 0}
    assert report["suggested_commands"][0] == "brigade work inbox"
    report_path = tmp_path / ".brigade" / "scanners" / "sweeps" / report["sweep_id"] / "sweep.json"
    assert report_path.is_file()

    assert work_cmd.import_list(target=tmp_path, json_output=True) == 0
    imports = json.loads(capsys.readouterr().out)["imports"]
    assert len(imports) == 1
    assert report["import_references"]["created_import_ids"] == [imports[0]["id"]]
    assert report["import_references"]["runs"][0]["scanner_id"] == "repo-scan"
    assert report["import_references"]["runs"][0]["scanner_run_id"] == report["scanner_run_ids"][0]
    assert imports[0]["metadata"]["scanner_run_id"] == report["scanner_run_ids"][0]
    assert imports[0]["metadata"]["scanner_receipt_path"] == report["receipt_paths"][0]

    assert work_cmd.sweep_review(target=tmp_path, sweep_id="latest", json_output=True) == 0
    review = json.loads(capsys.readouterr().out)
    assert review["sweep"]["sweep_id"] == report["sweep_id"]
    assert review["references"]["created_import_ids"] == [imports[0]["id"]]
    assert review["groups"] == [
        {
            "source": "repo-scan",
            "kind": "task",
            "priority": "normal",
            "acceptance_coverage": "ready",
            "provenance_status": "complete",
            "status": "pending",
            "count": 1,
            "import_ids": [imports[0]["id"]],
        }
    ]
    assert review["actionable_imports"][0]["suggested_commands"] == [
        f"brigade work import plan {imports[0]['id']}",
        f"brigade work import promote {imports[0]['id']}",
        f"brigade work import dismiss {imports[0]['id']} --reason \"...\"",
        f"brigade work import promote --run {imports[0]['id']}",
    ]

    assert work_cmd.sweep_review(target=tmp_path, sweep_id=report["sweep_id"]) == 0
    out = capsys.readouterr().out
    assert f"sweep_review: {report['sweep_id']}" in out
    assert "repo-scan task priority=normal acceptance=ready provenance=complete status=pending count=1" in out
    assert f"next: brigade work import plan {imports[0]['id']}" in out

    assert work_cmd.sweeps(target=tmp_path, json_output=True) == 0
    sweeps_payload = json.loads(capsys.readouterr().out)
    assert sweeps_payload["sweeps"][0]["sweep_id"] == report["sweep_id"]

    assert work_cmd.sweep_show(target=tmp_path, sweep_id=report["sweep_id"]) == 0
    out = capsys.readouterr().out
    assert f"sweep: {report['sweep_id']}" in out
    assert "status: completed" in out
    assert "created: 1" in out


def test_work_sweep_modes_disabled_no_ingest_and_failed_scanners(tmp_path, capsys):
    _init_git_repo(tmp_path)
    script = tmp_path / "scanner.py"
    script.write_text(
        """
import json
import sys
from pathlib import Path

if sys.argv[1] == "fail":
    print("failure", file=sys.stderr)
    raise SystemExit(4)
path = Path.cwd() / ".brigade" / f"{sys.argv[1]}.jsonl"
path.parent.mkdir(parents=True, exist_ok=True)
path.write_text(json.dumps({"kind": "task", "source": sys.argv[1], "text": f"Review {sys.argv[1]}"}) + "\\n")
"""
    )
    config = tmp_path / ".brigade" / "scanners.toml"
    config.parent.mkdir(parents=True)
    config.write_text(
        f"""
[[scanner]]
id = "enabled-scan"
source = "enabled-scan"
command = "{sys.executable} {script} enabled-scan"
cadence = "daily@02:00"
enabled = true
timeout = 30
output_path = ".brigade/enabled-scan.jsonl"
import_path = ".brigade/enabled-scan.jsonl"
import_format = "jsonl"
conflict_window = "02:00-02:10"

[[scanner]]
id = "disabled-scan"
source = "disabled-scan"
command = "{sys.executable} {script} disabled-scan"
cadence = "daily@03:00"
enabled = false
timeout = 30
output_path = ".brigade/disabled-scan.jsonl"
import_path = ".brigade/disabled-scan.jsonl"
import_format = "jsonl"
conflict_window = "03:00-03:10"

[[scanner]]
id = "fail-scan"
source = "fail-scan"
command = "{sys.executable} {script} fail"
cadence = "daily@04:00"
enabled = true
timeout = 30
output_path = ".brigade/fail.jsonl"
import_path = ".brigade/fail.jsonl"
import_format = "jsonl"
conflict_window = "04:00-04:10"
"""
    )

    assert work_cmd.sweep(target=tmp_path, scanner_id="enabled-scan", ingest=False, json_output=True) == 0
    no_ingest = json.loads(capsys.readouterr().out)
    assert no_ingest["ingest"] is False
    assert no_ingest["import_counts"]["created"] == 0
    assert work_cmd.import_list(target=tmp_path, json_output=True) == 0
    assert json.loads(capsys.readouterr().out)["imports"] == []

    assert work_cmd.sweep(target=tmp_path, scanner_id="disabled-scan", json_output=True) == 2
    disabled = json.loads(capsys.readouterr().out)
    assert disabled["status"] == "failed"
    assert disabled["errors"] == ["scanner disabled: disabled-scan"]

    assert work_cmd.sweep(target=tmp_path, scanner_id="disabled-scan", include_disabled=True, json_output=True) == 0
    included = json.loads(capsys.readouterr().out)
    assert included["status"] == "completed"
    assert included["import_counts"]["created"] == 1

    assert work_cmd.sweep(target=tmp_path, all_matching=True, include_disabled=True, json_output=True) == 1
    all_report = json.loads(capsys.readouterr().out)
    assert all_report["status"] == "failed"
    assert all_report["run_result"]["failed"] == 1
    assert any(run["scanner_id"] == "fail-scan" and run["status"] == "failed" for run in all_report["run_result"]["runs"])


def test_work_sweep_records_skipped_and_dismissed_fingerprints(tmp_path, capsys):
    _init_git_repo(tmp_path)
    pending = work_cmd._make_import(
        "Existing pending",
        kind="task",
        source="repo-scan",
        metadata={"source_item_key": "same-pending", "source_fingerprint": "fp-pending"},
    )
    dismissed = work_cmd._make_import(
        "Existing dismissed",
        kind="task",
        source="repo-scan",
        metadata={"source_item_key": "same-dismissed", "source_fingerprint": "fp-dismissed"},
    )
    dismissed["status"] = "dismissed"
    work_cmd._write_imports(tmp_path, [pending, dismissed])
    script = tmp_path / "scanner.py"
    script.write_text(
        """
import json
from pathlib import Path

records = [
    {
        "kind": "task",
        "source": "repo-scan",
        "text": "Existing pending",
        "metadata": {"source_item_key": "same-pending", "source_fingerprint": "fp-pending"},
    },
    {
        "kind": "task",
        "source": "repo-scan",
        "text": "Existing dismissed",
        "metadata": {"source_item_key": "same-dismissed", "source_fingerprint": "fp-dismissed"},
    },
]
path = Path.cwd() / ".brigade" / "scanner-imports.jsonl"
path.parent.mkdir(parents=True, exist_ok=True)
path.write_text("\\n".join(json.dumps(record) for record in records) + "\\n")
"""
    )
    config = tmp_path / ".brigade" / "scanners.toml"
    config.parent.mkdir(parents=True, exist_ok=True)
    config.write_text(
        f"""
[[scanner]]
id = "repo-scan"
source = "repo-scan"
command = "{sys.executable} {script}"
cadence = "daily@02:00"
enabled = true
timeout = 30
output_path = ".brigade/scanner-imports.jsonl"
import_path = ".brigade/scanner-imports.jsonl"
import_format = "jsonl"
conflict_window = "02:00-02:10"
"""
    )

    assert work_cmd.sweep(target=tmp_path, scanner_id="repo-scan", json_output=True) == 0
    report = json.loads(capsys.readouterr().out)
    assert report["import_counts"] == {"created": 0, "dismissed": 1, "skipped": 1}
    assert report["import_references"]["created_import_ids"] == []
    assert report["import_references"]["skipped_source_fingerprints"] == ["fp-pending"]
    assert report["import_references"]["dismissed_source_fingerprints"] == ["fp-dismissed"]

    assert work_cmd.sweep_review(target=tmp_path, sweep_id=report["sweep_id"], json_output=True) == 0
    review = json.loads(capsys.readouterr().out)
    assert review["references"]["skipped_source_fingerprints"] == ["fp-pending"]
    assert review["references"]["dismissed_source_fingerprints"] == ["fp-dismissed"]
    assert any(check["name"] == "scanner_sweep_noisy_noop" and check["status"] == "warn" for check in review["checks"])


def test_work_brief_and_doctor_include_scanner_sweep_health(tmp_path, monkeypatch, capsys):
    _init_git_repo(tmp_path)
    dogfood_cmd.init(target=tmp_path)
    monkeypatch.setattr(work_cmd.shutil, "which", lambda name: f"/usr/bin/{name}")
    monkeypatch.setattr(
        work_cmd,
        "_now",
        lambda: datetime(2026, 5, 30, 12, 0, 0, tzinfo=timezone.utc),
    )
    config = tmp_path / ".brigade" / "scanners.toml"
    config.parent.mkdir(parents=True, exist_ok=True)
    config.write_text(
        """
[[scanner]]
id = "due-scan"
source = "due-scan"
command = "python3 scanner.py"
cadence = "daily@02:00"
enabled = true
timeout = 30
output_path = ".brigade/due.jsonl"
import_path = ".brigade/due.jsonl"
import_format = "jsonl"
conflict_window = "02:00-02:10"
"""
    )
    sweep_dir = tmp_path / ".brigade" / "scanners" / "sweeps" / "old-failed"
    sweep_dir.mkdir(parents=True)
    _write_json(
        sweep_dir / "sweep.json",
        {
            "sweep_id": "old-failed",
            "status": "failed",
            "started_at": "2026-05-25T12:00:00+00:00",
            "completed_at": "2026-05-25T12:00:00+00:00",
            "scanner_run_ids": [],
            "import_counts": {"created": 0, "skipped": 0, "dismissed": 0},
        },
    )

    assert work_cmd.brief(target=tmp_path) == 0
    out = capsys.readouterr().out
    assert "scanner_latest_sweep: old-failed [failed]" in out
    assert "scanner_sweep_command: brigade work sweep" in out

    assert work_cmd.doctor(target=tmp_path) == 0
    out = capsys.readouterr().out
    assert "[warn] scanner_sweep_failed: old-failed" in out
    assert "[warn] scanner_sweep_stale: old-failed=120.0h" in out


def test_work_sweep_review_health_reports_missing_provenance_and_stale_review(tmp_path, monkeypatch, capsys):
    _init_git_repo(tmp_path)
    dogfood_cmd.init(target=tmp_path)
    capsys.readouterr()
    monkeypatch.setattr(work_cmd.shutil, "which", lambda name: f"/usr/bin/{name}")
    monkeypatch.setattr(
        work_cmd,
        "_now",
        lambda: datetime(2026, 5, 30, 12, 0, 0, tzinfo=timezone.utc),
    )
    config = tmp_path / ".brigade" / "scanners.toml"
    config.parent.mkdir(parents=True, exist_ok=True)
    config.write_text(
        """
[[scanner]]
id = "repo-scan"
source = "repo-scan"
command = "python3 scanner.py"
cadence = "daily@02:00"
enabled = true
timeout = 30
output_path = ".brigade/due.jsonl"
import_path = ".brigade/due.jsonl"
import_format = "jsonl"
conflict_window = "02:00-02:10"
"""
    )
    item = work_cmd._make_import(
        "Review stale sweep import",
        kind="task",
        source="repo-scan",
        metadata={"scanner_id": "repo-scan"},
    )
    work_cmd._write_imports(tmp_path, [item])
    sweep_dir = tmp_path / ".brigade" / "scanners" / "sweeps" / "stale-review"
    sweep_dir.mkdir(parents=True)
    _write_json(
        sweep_dir / "sweep.json",
        {
            "sweep_id": "stale-review",
            "status": "completed",
            "started_at": "2026-05-28T10:00:00+00:00",
            "completed_at": "2026-05-28T10:00:00+00:00",
            "scanner_run_ids": ["run-1"],
            "import_counts": {"created": 2, "skipped": 0, "dismissed": 0},
            "import_references": {
                "created_import_ids": [item["id"], "missing-import"],
                "skipped_source_fingerprints": [],
                "dismissed_source_fingerprints": [],
                "runs": [
                    {
                        "scanner_id": "repo-scan",
                        "scanner_source": "repo-scan",
                        "scanner_run_id": "run-1",
                        "receipt_path": str(tmp_path / ".brigade" / "scanners" / "runs" / "run-1" / "receipt.json"),
                        "import_path": str(tmp_path / ".brigade" / "due.jsonl"),
                        "created_import_ids": [item["id"], "missing-import"],
                        "skipped_source_fingerprints": [],
                        "dismissed_source_fingerprints": [],
                    }
                ],
            },
        },
    )

    assert work_cmd.sweep_review(target=tmp_path, sweep_id="stale-review", json_output=True) == 0
    review = json.loads(capsys.readouterr().out)
    issue_names = {check["name"] for check in review["issues"]}
    assert "scanner_sweep_unreviewed" in issue_names
    assert "scanner_sweep_missing_imports" in issue_names
    assert "scanner_sweep_missing_provenance" in issue_names
    assert review["missing_import_ids"] == ["missing-import"]

    assert work_cmd.brief(target=tmp_path) == 0
    out = capsys.readouterr().out
    assert "scanner_unreviewed_sweep: stale-review" in out
    assert f"scanner_sweep_import: {item['id']} repo-scan Review stale sweep import" in out

    assert work_cmd.doctor(target=tmp_path) == 0
    out = capsys.readouterr().out
    assert "[warn] scanner_sweep_unreviewed: 1 pending sweep import(s) older than 24h" in out
    assert "[warn] scanner_sweep_missing_imports: 1 sweep import reference(s) missing from inbox" in out
    assert "[warn] scanner_sweep_missing_provenance: 1 sweep import(s) missing scanner provenance" in out

    assert work_cmd.inbox_doctor(target=tmp_path) == 0
    out = capsys.readouterr().out
    assert "[warn] inbox_sweep_import_missing: 1 sweep import reference(s) missing from inbox" in out
    assert "[warn] inbox_sweep_import_provenance: 1 sweep import reference(s) lost provenance" in out


def test_work_scanners_doctor_warns_for_missing_stale_bad_and_imports_issues(tmp_path, monkeypatch, capsys):
    _init_git_repo(tmp_path)
    monkeypatch.setattr(
        work_cmd,
        "_now",
        lambda: datetime(2026, 5, 30, 12, 0, 0, tzinfo=timezone.utc),
    )
    output = tmp_path / ".brigade" / "chat-memory-sweeps" / "latest.json"
    output.parent.mkdir(parents=True)
    output.write_text("{}\n")
    old = datetime(2026, 5, 25, 12, 0, 0, tzinfo=timezone.utc).timestamp()
    os.utime(output, (old, old))
    config = tmp_path / ".brigade" / "scanners.toml"
    config.write_text(
        f"""
[[scanner]]
id = "chat-memory-sweep"
source = "chat-memory-sweep"
command = "missing-scanner-command --flag"
cadence = "daily@02:00"
enabled = true
timeout = 300
output_path = "{output.relative_to(tmp_path)}"
conflict_window = "02:00-02:30"
"""
    )

    assert work_cmd.scanners_doctor(target=tmp_path, import_issues=True) == 0
    out = capsys.readouterr().out
    assert "[warn] scanner_required:" in out
    assert "[warn] scanner_commands: chat-memory-sweep" in out
    assert "[warn] scanner_outputs: stale=chat-memory-sweep=120.0h" in out
    assert "imported_issues:" in out
    assert work_cmd.import_list(target=tmp_path, json_output=True) == 0
    imports = json.loads(capsys.readouterr().out)["imports"]
    assert any(item["source"] == "scanner-health" for item in imports)

    assert work_cmd.scanners_doctor(target=tmp_path, json_output=True) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["checks"]
    assert payload["import_issues"] if "import_issues" in payload else True


def test_work_brief_and_doctor_include_scanner_health(tmp_path, monkeypatch, capsys):
    _init_git_repo(tmp_path)
    dogfood_cmd.init(target=tmp_path)
    monkeypatch.setattr(work_cmd.shutil, "which", lambda name: f"/usr/bin/{name}")
    monkeypatch.setattr(dogfood_cmd, "_check_git_ignored", lambda repo, path: "yes")
    assert work_cmd.scanners_init(target=tmp_path, update_gitignore=False) == 0
    capsys.readouterr()

    assert work_cmd.brief(target=tmp_path) == 0
    out = capsys.readouterr().out
    assert "scanner_config:" in out
    assert "scanner_health:" in out
    assert "scanner_next_run:" in out

    assert work_cmd.doctor(target=tmp_path) == 0
    out = capsys.readouterr().out
    assert "[ok] scanner_config:" in out
    assert "[ok] scanner_required:" in out
    assert "[warn] scanner_outputs:" in out


def test_work_brief_and_doctor_include_security_health(tmp_path, monkeypatch, capsys):
    _init_git_repo(tmp_path)
    dogfood_cmd.init(target=tmp_path)
    monkeypatch.setattr(work_cmd.shutil, "which", lambda name: f"/usr/bin/{name}")
    monkeypatch.setattr(dogfood_cmd, "_check_git_ignored", lambda repo, path: "yes")
    assert security_cmd.init(target=tmp_path) == 0
    (tmp_path / ".env").write_text("SERVICE_TOKEN=abcd1234abcd1234abcd1234\n")
    assert security_cmd.scan(target=tmp_path, fail_on="none") == 0
    capsys.readouterr()

    assert work_cmd.brief(target=tmp_path) == 0
    out = capsys.readouterr().out
    assert "security_config:" in out
    assert "security_health:" in out
    assert "security_top_finding:" in out

    assert work_cmd.doctor(target=tmp_path) == 0
    out = capsys.readouterr().out
    assert "[warn] security_open_findings:" in out


def test_work_brief_and_doctor_include_memory_care_health(tmp_path, monkeypatch, capsys):
    from brigade import memory_cmd

    _init_git_repo(tmp_path)
    dogfood_cmd.init(target=tmp_path)
    monkeypatch.setattr(work_cmd.shutil, "which", lambda name: f"/usr/bin/{name}")
    monkeypatch.setattr(dogfood_cmd, "_check_git_ignored", lambda repo, path: "yes")
    monkeypatch.setattr(memory_cmd, "_today", lambda: date(2026, 5, 28))
    assert memory_cmd.init(target=tmp_path, update_gitignore=False) == 0
    cards = tmp_path / "memory" / "cards"
    cards.mkdir(parents=True)
    (cards / "stale.md").write_text(
        "\n".join(
            [
                "---",
                "topic: stale",
                "last_reviewed: 2026-01-01",
                "confidence: high",
                'evidence: ["README.md"]',
                "---",
                "",
                "Body.",
            ]
        )
    )
    (tmp_path / "MEMORY.md").write_text("- [stale](memory/cards/stale.md)\n")
    assert memory_cmd.scan(target=tmp_path) == 0
    capsys.readouterr()

    assert work_cmd.brief(target=tmp_path) == 0
    out = capsys.readouterr().out
    assert "memory_care_config:" in out
    assert "memory_care_health:" in out
    assert "memory_care_top_issue:" in out

    assert work_cmd.doctor(target=tmp_path) == 0
    out = capsys.readouterr().out
    assert "[warn] memory_care_open_issues:" in out


def test_work_import_plan_previews_promoted_task(tmp_path, monkeypatch, capsys):
    _init_git_repo(tmp_path)
    monkeypatch.setattr(
        work_cmd,
        "_now",
        lambda: datetime(2026, 5, 26, 12, 0, 0, tzinfo=timezone.utc),
    )
    import_file = tmp_path / "task-imports.jsonl"
    import_file.write_text(
        json.dumps(
            {
                "text": "Plan scanner task",
                "kind": "task",
                "source": "repo-scan",
                "type": "feature",
                "priority": "urgent",
                "template": "bugfix",
                "acceptance": ["Scanner acceptance."],
                "metadata": {"scanner": "daily"},
            }
        )
        + "\n"
    )
    assert work_cmd.import_ingest(target=tmp_path, input_path=import_file) == 0
    item = json.loads((tmp_path / ".brigade" / "work" / "imports" / "inbox.jsonl").read_text().splitlines()[0])
    capsys.readouterr()

    assert work_cmd.import_plan(target=tmp_path, import_id=item["id"]) == 0
    out = capsys.readouterr().out
    assert "task:" in out
    assert "type: feature" in out
    assert "priority: urgent" in out
    assert "template: bugfix" in out
    assert "acceptance: 4" in out
    assert "The bug is reproduced by a focused failing test" in out
    assert "Scanner acceptance." in out
    assert f"run: brigade work import promote --run {item['id']}" in out

    assert work_cmd.import_plan(target=tmp_path, import_id=item["id"], json_output=True) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["task"]["type"] == "feature"
    assert payload["task"]["priority"] == "urgent"
    assert payload["task"]["metadata"]["scanner"] == "daily"
    assert payload["guidance"] == list(work_cmd.TASK_TEMPLATES["bugfix"]["guidance"])


def test_work_import_validate_reports_schema_errors(tmp_path, capsys):
    import_file = tmp_path / "bad-imports.jsonl"
    import_file.write_text('{"kind":"nope","metadata":[]}\nnot-json\n')

    assert work_cmd.import_validate(input_path=import_file) == 1
    out = capsys.readouterr().out
    assert "errors: 4" in out
    assert "line 1: text must be a non-empty string" in out
    assert "line 1: kind must be one of:" in out
    assert "line 1: metadata must be an object when present" in out

    assert work_cmd.import_validate(input_path=import_file, json_output=True) == 1
    payload = json.loads(capsys.readouterr().out)
    assert payload["valid"] is False
    assert len(payload["errors"]) == 4


def test_work_import_validate_rejects_bad_task_fields(tmp_path, capsys):
    import_file = tmp_path / "bad-task-imports.jsonl"
    import_file.write_text(
        json.dumps(
            {
                "text": "Bad task import",
                "kind": "task",
                "source": "scanner",
                "type": "invalid",
                "priority": "now",
                "template": "unknown",
                "acceptance": "not-a-list",
            }
        )
        + "\n"
        + json.dumps(
            {
                "text": "Wrong kind",
                "kind": "finding",
                "source": "scanner",
                "acceptance": ["Only tasks may carry acceptance."],
            }
        )
        + "\n"
        + json.dumps(
            {
                "text": "Empty acceptance",
                "kind": "task",
                "source": "scanner",
                "acceptance": [""],
            }
        )
        + "\n"
    )

    assert work_cmd.import_validate(input_path=import_file) == 1
    out = capsys.readouterr().out
    assert "line 1: type must be one of:" in out
    assert "line 1: priority must be one of:" in out
    assert "line 1: template must be one of:" in out
    assert "line 1: acceptance must be a list of non-empty strings" in out
    assert "line 2: task fields are only valid when kind is task" in out
    assert "line 3: acceptance item 1 must be a non-empty string" in out


def test_work_import_memory_care_reads_refresh_queue(tmp_path, monkeypatch, capsys):
    _init_git_repo(tmp_path)
    monkeypatch.setattr(
        work_cmd,
        "_now",
        lambda: datetime(2026, 5, 26, 12, 0, 0, tzinfo=timezone.utc),
    )
    queue = tmp_path / "memory" / "cards" / "decay" / "refresh-queue.json"
    queue.parent.mkdir(parents=True)
    queue.write_text(
        json.dumps(
            {
                "cards": [
                    {
                        "file": "memory/cards/tools.md",
                        "reason": "source-of-truth changed",
                    }
                ]
            }
        )
    )

    assert work_cmd.import_memory_care(target=tmp_path) == 0
    out = capsys.readouterr().out
    assert f"memory-care queue: {queue}" in out
    assert "queued_cards: 1" in out
    assert "imported: 1" in out
    assert work_cmd.import_memory_care(target=tmp_path) == 0
    out = capsys.readouterr().out
    assert "imported: 0" in out
    assert "skipped_duplicates: 1" in out

    assert work_cmd.import_list(target=tmp_path, json_output=True) == 0
    payload = json.loads(capsys.readouterr().out)
    item = payload["imports"][0]
    assert item["kind"] == "task"
    assert item["source"] == "memory-care"
    assert item["text"] == "Refresh memory card memory/cards/tools.md: source-of-truth changed"
    assert item["metadata"]["card_file"] == "memory/cards/tools.md"
    assert item["metadata"]["reason"] == "source-of-truth changed"


def test_work_import_chat_sweep_reads_issues(tmp_path, monkeypatch, capsys):
    _init_git_repo(tmp_path)
    monkeypatch.setattr(
        work_cmd,
        "_now",
        lambda: datetime(2026, 5, 26, 12, 0, 0, tzinfo=timezone.utc),
    )
    sweep = tmp_path / ".brigade" / "chat-memory-sweeps" / "latest.json"
    sweep.parent.mkdir(parents=True)
    _write_json(
        sweep,
        {
            "generated_at": "2026-05-26T22:09:00-04:00",
            "sessions": {"listed": 24, "reviewed": 10, "durable": 1},
            "issues": [
                {
                    "title": "Cron delivery failure",
                    "summary": "Recent message delivery failed.",
                    "kind": "incident",
                    "source": "cron",
                    "severity": "warning",
                    "metadata": {
                        "surface": "discord",
                        "local_locator": "crawler://discord/example",
                    },
                }
            ],
        },
    )

    assert work_cmd.import_chat_sweep(target=tmp_path) == 0
    out = capsys.readouterr().out
    assert f"chat memory sweep: {sweep}" in out
    assert "issues: 1" in out
    assert "imported: 1" in out
    assert work_cmd.import_chat_sweep(target=tmp_path) == 0
    out = capsys.readouterr().out
    assert "imported: 0" in out
    assert "skipped_duplicates: 1" in out

    assert work_cmd.import_list(target=tmp_path, json_output=True) == 0
    payload = json.loads(capsys.readouterr().out)
    item = payload["imports"][0]
    assert item["kind"] == "incident"
    assert item["source"] == "chat-memory-sweep"
    assert item["text"] == "Review memory sweep issue [warning] Cron delivery failure: Recent message delivery failed."
    assert item["metadata"]["surface"] == "discord"
    assert item["metadata"]["issue_source"] == "cron"
    assert item["metadata"]["severity"] == "warning"
    assert item["metadata"]["sweep_path"] == str(sweep)


def test_work_import_chat_sweep_actionable_task_privacy_and_idempotency(tmp_path, monkeypatch, capsys):
    _init_git_repo(tmp_path)
    times = iter(
        [
            datetime(2026, 5, 26, 12, 0, 0, tzinfo=timezone.utc),
            datetime(2026, 5, 26, 12, 1, 0, tzinfo=timezone.utc),
            datetime(2026, 5, 26, 12, 2, 0, tzinfo=timezone.utc),
            datetime(2026, 5, 26, 12, 3, 0, tzinfo=timezone.utc),
        ]
    )
    monkeypatch.setattr(work_cmd, "_now", lambda: next(times))
    sweep = tmp_path / ".brigade" / "chat-memory-sweeps" / "latest.json"
    sweep.parent.mkdir(parents=True)
    _write_json(
        sweep,
        {
            "sweep_id": "nightly-2026-05-26",
            "provider": "openclaw",
            "generated_at": "2026-05-26T22:09:00-04:00",
            "issues": [
                {
                    "id": "issue-1",
                    "title": "Memory ingest warning",
                    "summary": "Ingest skipped one handoff.",
                    "actionable": True,
                    "priority": "high",
                    "confidence": "high",
                    "evidence_summary": "NO_REPLY warning in local sweep artifact.",
                    "raw_text": "PRIVATE CHAT TRANSCRIPT",
                    "metadata": {
                        "workspace": "ops",
                        "channel": "memory",
                        "thread": "abc123",
                        "message_range": "42-44",
                        "raw_messages": ["PRIVATE CHAT MESSAGE"],
                    },
                    "acceptance": ["Repair or document the ingest warning."],
                }
            ],
        },
    )

    assert work_cmd.import_chat_sweep(target=tmp_path, json_output=True) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["created"] == 1
    assert payload["skipped"] == 0
    assert payload["dismissed"] == 0
    assert payload["invalid"] == 0
    item = payload["imports"][0]
    rendered = json.dumps(item, sort_keys=True)
    assert item["kind"] == "task"
    assert item["priority"] == "high"
    assert item["template"] == "vertical-slice"
    assert item["acceptance"] == ["Repair or document the ingest warning."]
    assert item["metadata"]["provider"] == "openclaw"
    assert item["metadata"]["workspace"] == "ops"
    assert item["metadata"]["channel"] == "memory"
    assert item["metadata"]["thread"] == "abc123"
    assert item["metadata"]["message_range"] == "42-44"
    assert item["metadata"]["confidence"] == "high"
    assert item["metadata"]["evidence_summary"] == "NO_REPLY warning in local sweep artifact."
    assert "PRIVATE CHAT" not in rendered
    assert item["metadata"]["private_fields_omitted"] == ["raw_messages", "raw_text"]

    assert work_cmd.import_chat_sweep(target=tmp_path, json_output=True) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["created"] == 0
    assert payload["skipped"] == 1

    import_id = item["id"]
    assert work_cmd.import_dismiss(target=tmp_path, import_id=import_id, reason="not now") == 0
    capsys.readouterr()
    assert work_cmd.import_chat_sweep(target=tmp_path, json_output=True) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["created"] == 0
    assert payload["dismissed"] == 1

    data = json.loads(sweep.read_text())
    data["issues"][0]["summary"] = "Ingest skipped two handoffs."
    _write_json(sweep, data)
    assert work_cmd.import_chat_sweep(target=tmp_path, json_output=True) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["created"] == 1


def test_work_import_chat_sweep_reports_precise_errors(tmp_path, capsys):
    _init_git_repo(tmp_path)
    sweep = tmp_path / "bad-sweep.json"
    _write_json(
        sweep,
        {
            "issues": [
                {"summary": "missing title"},
                {"title": "Bad kind", "kind": "bad"},
                {"title": "Bad metadata", "metadata": []},
                "not-object",
            ]
        },
    )

    assert work_cmd.import_chat_sweep(target=tmp_path, input_path=sweep) == 2
    err = capsys.readouterr().err
    assert "chat memory sweep issue 1 requires title" in err
    assert "chat memory sweep issue 2 kind must be one of:" in err
    assert "chat memory sweep issue 3 metadata must be an object" in err
    assert "chat memory sweep issue 4 must be an object" in err

    assert work_cmd.import_chat_sweep(target=tmp_path, input_path=sweep, json_output=True) == 2
    payload = json.loads(capsys.readouterr().out)
    assert payload["valid"] is False
    assert payload["created"] == 0
    assert payload["invalid"] == 4
    assert len(payload["errors"]) == 4


def test_work_import_memory_refresh_reads_candidates(tmp_path, monkeypatch, capsys):
    _init_git_repo(tmp_path)
    monkeypatch.setattr(
        work_cmd,
        "_now",
        lambda: datetime(2026, 5, 26, 12, 0, 0, tzinfo=timezone.utc),
    )
    queue = tmp_path / "memory-refresh.json"
    _write_json(
        queue,
        {
            "refresh_candidates": [
                {
                    "id": "tools-card",
                    "file": "memory/cards/tools.md",
                    "refresh_reason": "contradictory tool notes",
                    "confidence": "high",
                    "evidence_summary": "Two recent handoffs disagree.",
                    "priority": "high",
                }
            ]
        },
    )

    assert work_cmd.import_memory_refresh(target=tmp_path, queue=queue, json_output=True) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["created"] == 1
    item = payload["imports"][0]
    assert item["source"] == "memory-refresh"
    assert item["kind"] == "task"
    assert item["type"] == "docs"
    assert item["priority"] == "high"
    assert item["template"] == "docs"
    assert item["metadata"]["card_id"] == "tools-card"
    assert item["metadata"]["card_file"] == "memory/cards/tools.md"
    assert item["metadata"]["refresh_reason"] == "contradictory tool notes"
    assert item["metadata"]["confidence"] == "high"
    assert item["metadata"]["evidence_summary"] == "Two recent handoffs disagree."
    assert item["acceptance"] == [
        "Review memory/cards/tools.md against current source evidence.",
        "Update the memory card or document why no change is needed.",
    ]


def test_work_chat_sweep_flows_through_inbox_plan_promote_run_completion(tmp_path, monkeypatch, capsys):
    _init_git_repo(tmp_path)
    artifacts_dir = tmp_path / ".brigade" / "runs"
    dogfood_cmd.init(target=tmp_path, artifacts_dir=artifacts_dir)
    times = iter(
        [
            datetime(2026, 5, 26, 12, 0, 0, tzinfo=timezone.utc),
            datetime(2026, 5, 26, 12, 1, 0, tzinfo=timezone.utc),
            datetime(2026, 5, 26, 12, 2, 0, tzinfo=timezone.utc),
            datetime(2026, 5, 26, 12, 3, 0, tzinfo=timezone.utc),
            datetime(2026, 5, 26, 12, 4, 0, tzinfo=timezone.utc),
            datetime(2026, 5, 26, 12, 5, 0, tzinfo=timezone.utc),
            datetime(2026, 5, 26, 12, 6, 0, tzinfo=timezone.utc),
            datetime(2026, 5, 26, 12, 7, 0, tzinfo=timezone.utc),
        ]
    )
    monkeypatch.setattr(work_cmd, "_now", lambda: next(times))
    sweep = tmp_path / ".brigade" / "chat-memory-sweeps" / "latest.json"
    sweep.parent.mkdir(parents=True)
    _write_json(
        sweep,
        {
            "sweep_id": "nightly-2026-05-26",
            "issues": [
                {
                    "id": "action-1",
                    "title": "Repair memory sweep ingestion",
                    "summary": "One local warning needs review.",
                    "actionable": True,
                    "confidence": "high",
                    "priority": "urgent",
                    "acceptance": ["The warning is resolved or documented."],
                }
            ],
        },
    )

    assert work_cmd.import_chat_sweep(target=tmp_path) == 0
    item = json.loads((tmp_path / ".brigade" / "work" / "imports" / "inbox.jsonl").read_text().splitlines()[0])
    capsys.readouterr()
    assert work_cmd.inbox(target=tmp_path) == 0
    out = capsys.readouterr().out
    assert f"import: {item['id']}" in out
    assert "confidence=high" in out

    assert work_cmd.import_plan(target=tmp_path, import_id=item["id"]) == 0
    out = capsys.readouterr().out
    assert "The warning is resolved or documented." in out
    assert "sweep_issue_id: action-1" in out

    def fake_dogfood_run(task, **kwargs):
        assert "Repair memory sweep ingestion" in task
        assert "The warning is resolved or documented." in task
        run_dir = kwargs["output_dir"] or artifacts_dir / "chat-sweep-run"
        run_dir.mkdir(parents=True)
        _write_json(run_dir / "run.json", {"started_at": "2026-05-26T12:03:00Z", "status": "ok", "task": task})
        (run_dir / "final.txt").write_text("Done.\n")
        return 0

    monkeypatch.setattr(dogfood_cmd, "run", fake_dogfood_run)

    assert work_cmd.import_promote(target=tmp_path, import_id=item["id"], run_after=True) == 0
    ledger = json.loads((tmp_path / ".brigade" / "work" / "tasks.json").read_text())
    task = ledger["tasks"][0]
    assert task["status"] == "done"
    assert task["source"] == "import:chat-memory-sweep"
    assert task["metadata"]["sweep_issue_id"] == "action-1"
    assert task["completed_acceptance"] == [
        "One user-visible path is implemented end to end.",
        "Focused tests cover the new path.",
        "Documentation or help text is updated when user behavior changes.",
        "The warning is resolved or documented.",
    ]


def test_work_import_triage_groups_pending_imports(tmp_path, monkeypatch, capsys):
    _init_git_repo(tmp_path)
    monkeypatch.setattr(
        work_cmd,
        "_now",
        lambda: datetime(2026, 5, 26, 12, 0, 0, tzinfo=timezone.utc),
    )
    assert work_cmd.import_add(target=tmp_path, text="Refresh card", kind="task", source="memory-care") == 0
    assert work_cmd.import_add(target=tmp_path, text="Check chat decision", kind="decision", source="slack") == 0
    assert work_cmd.import_add(target=tmp_path, text="Review chat task", kind="task", source="slack") == 0
    capsys.readouterr()

    assert work_cmd.import_triage(target=tmp_path) == 0
    out = capsys.readouterr().out
    assert "work import triage:" in out
    assert "pending_imports: 3" in out
    assert "- memory-care: 1" in out
    assert "  task: 1" in out
    assert "- slack: 2" in out
    assert "  decision: 1" in out
    assert "Review chat task" in out

    assert work_cmd.import_triage(target=tmp_path, json_output=True) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["counts"]["total"] == 3
    assert payload["counts"]["by_source"] == {"memory-care": 1, "slack": 2}
    assert payload["counts"]["by_kind"] == {"decision": 1, "task": 2}
    assert payload["groups"]["slack"]["decision"][0]["text"] == "Check chat decision"


def test_work_import_list_and_triage_filter_by_metadata(tmp_path, monkeypatch, capsys):
    _init_git_repo(tmp_path)
    monkeypatch.setattr(
        work_cmd,
        "_now",
        lambda: datetime(2026, 5, 26, 12, 0, 0, tzinfo=timezone.utc),
    )
    assert (
        work_cmd.import_add(
            target=tmp_path,
            text="Repair skipped handoff",
            kind="task",
            source="handoff-ingest",
            metadata=["handoff_issue_category=skip"],
        )
        == 0
    )
    assert (
        work_cmd.import_add(
            target=tmp_path,
            text="Repair route skip",
            kind="task",
            source="handoff-ingest",
            metadata=["handoff_issue_category=route-skip"],
        )
        == 0
    )
    capsys.readouterr()

    assert (
        work_cmd.import_list(
            target=tmp_path,
            json_output=True,
            source="handoff-ingest",
            kind="task",
            metadata=["handoff_issue_category=skip"],
        )
        == 0
    )
    payload = json.loads(capsys.readouterr().out)
    assert [item["text"] for item in payload["imports"]] == ["Repair skipped handoff"]

    assert (
        work_cmd.import_triage(
            target=tmp_path,
            json_output=True,
            source="handoff-ingest",
            metadata=["handoff_issue_category=route-skip"],
        )
        == 0
    )
    payload = json.loads(capsys.readouterr().out)
    assert payload["counts"]["total"] == 1
    assert payload["groups"]["handoff-ingest"]["task"][0]["text"] == "Repair route skip"


def test_work_import_promote_all_filters_by_source_and_kind(tmp_path, monkeypatch, capsys):
    _init_git_repo(tmp_path)
    monkeypatch.setattr(
        work_cmd,
        "_now",
        lambda: datetime(2026, 5, 26, 12, 0, 0, tzinfo=timezone.utc),
    )
    assert work_cmd.import_add(target=tmp_path, text="Refresh card one", kind="task", source="memory-care") == 0
    assert work_cmd.import_add(target=tmp_path, text="Refresh card two", kind="task", source="memory-care") == 0
    assert work_cmd.import_add(target=tmp_path, text="Review chat note", kind="task", source="slack") == 0
    assert work_cmd.import_add(target=tmp_path, text="Record decision", kind="decision", source="memory-care") == 0
    capsys.readouterr()

    assert (
        work_cmd.import_promote(
            target=tmp_path,
            all_matching=True,
            kind="task",
            source="memory-care",
        )
        == 0
    )
    out = capsys.readouterr().out
    assert "promoted: 2" in out
    assert "created: 2" in out
    assert "existing: 0" in out
    ledger = json.loads((tmp_path / ".brigade" / "work" / "tasks.json").read_text())
    assert [task["text"] for task in ledger["tasks"]] == ["Refresh card one", "Refresh card two"]

    assert work_cmd.import_list(target=tmp_path, json_output=True) == 0
    payload = json.loads(capsys.readouterr().out)
    assert [item["text"] for item in payload["imports"]] == ["Review chat note", "Record decision"]


def test_work_import_promote_all_preserves_task_metadata(tmp_path, monkeypatch, capsys):
    _init_git_repo(tmp_path)
    times = iter(
        [
            datetime(2026, 5, 26, 12, 0, 0, tzinfo=timezone.utc),
            datetime(2026, 5, 26, 12, 1, 0, tzinfo=timezone.utc),
            datetime(2026, 5, 26, 12, 1, 1, tzinfo=timezone.utc),
            datetime(2026, 5, 26, 12, 1, 2, tzinfo=timezone.utc),
            datetime(2026, 5, 26, 12, 1, 3, tzinfo=timezone.utc),
            datetime(2026, 5, 26, 12, 1, 4, tzinfo=timezone.utc),
        ]
    )
    monkeypatch.setattr(work_cmd, "_now", lambda: next(times))
    import_file = tmp_path / "task-imports.jsonl"
    records = [
        {
            "text": "Build scanner task one",
            "kind": "task",
            "source": "repo-scan",
            "type": "bug",
            "priority": "high",
            "acceptance": ["Bug fix acceptance."],
        },
        {
            "text": "Build scanner task two",
            "kind": "task",
            "source": "repo-scan",
            "type": "docs",
            "priority": "low",
            "acceptance": ["Docs acceptance."],
        },
    ]
    import_file.write_text("".join(json.dumps(record) + "\n" for record in records))
    assert work_cmd.import_ingest(target=tmp_path, input_path=import_file) == 0
    capsys.readouterr()

    assert work_cmd.import_promote(target=tmp_path, all_matching=True, source="repo-scan", kind="task") == 0
    out = capsys.readouterr().out
    assert "promoted: 2" in out
    assert "acceptance=1" in out
    ledger = json.loads((tmp_path / ".brigade" / "work" / "tasks.json").read_text())
    assert [(task["type"], task["priority"], task["acceptance"]) for task in ledger["tasks"]] == [
        ("bug", "high", ["Bug fix acceptance."]),
        ("docs", "low", ["Docs acceptance."]),
    ]


def test_work_run_uses_promoted_import_acceptance(tmp_path, monkeypatch, capsys):
    _init_git_repo(tmp_path)
    artifacts_dir = tmp_path / ".brigade" / "runs"
    dogfood_cmd.init(target=tmp_path, artifacts_dir=artifacts_dir)
    times = iter(
        [
            datetime(2026, 5, 26, 12, 0, 0, tzinfo=timezone.utc),
            datetime(2026, 5, 26, 12, 1, 0, tzinfo=timezone.utc),
            datetime(2026, 5, 26, 12, 1, 1, tzinfo=timezone.utc),
            datetime(2026, 5, 26, 13, 0, 0, tzinfo=timezone.utc),
            datetime(2026, 5, 26, 13, 0, 1, tzinfo=timezone.utc),
            datetime(2026, 5, 26, 13, 0, 2, tzinfo=timezone.utc),
        ]
    )
    monkeypatch.setattr(work_cmd, "_now", lambda: next(times))
    import_file = tmp_path / "task-imports.jsonl"
    import_file.write_text(
        json.dumps(
            {
                "text": "Run promoted scanner task",
                "kind": "task",
                "source": "repo-scan",
                "acceptance": ["Promoted scanner acceptance reaches dogfood."],
            }
        )
        + "\n"
    )
    assert work_cmd.import_ingest(target=tmp_path, input_path=import_file) == 0
    assert work_cmd.import_promote(target=tmp_path, all_matching=True, source="repo-scan", kind="task") == 0
    capsys.readouterr()
    seen = {}

    def fake_dogfood_run(task, **kwargs):
        seen["task"] = task
        run_dir = kwargs["output_dir"]
        run_dir.mkdir(parents=True)
        _write_json(run_dir / "run.json", {"started_at": "2026-05-26T12:10:00Z", "status": "ok", "task": task})
        (run_dir / "final.txt").write_text("Done.\n\nNext step: Build follow-up.\n")
        return 0

    monkeypatch.setattr(dogfood_cmd, "run", fake_dogfood_run)

    assert work_cmd.run(None, target=tmp_path, output_dir=artifacts_dir / "new", handoff=False) == 0
    assert seen["task"].startswith("Run promoted scanner task")
    assert "- Promoted scanner acceptance reaches dogfood." in seen["task"]


def test_work_import_promote_run_success_records_completion(tmp_path, monkeypatch, capsys):
    _init_git_repo(tmp_path)
    artifacts_dir = tmp_path / ".brigade" / "runs"
    dogfood_cmd.init(target=tmp_path, artifacts_dir=artifacts_dir)
    times = iter(
        [
            datetime(2026, 5, 26, 12, 0, 0, tzinfo=timezone.utc),
            datetime(2026, 5, 26, 12, 1, 0, tzinfo=timezone.utc),
            datetime(2026, 5, 26, 12, 1, 1, tzinfo=timezone.utc),
            datetime(2026, 5, 26, 13, 0, 0, tzinfo=timezone.utc),
            datetime(2026, 5, 26, 13, 0, 1, tzinfo=timezone.utc),
            datetime(2026, 5, 26, 13, 0, 2, tzinfo=timezone.utc),
            datetime(2026, 5, 26, 13, 0, 3, tzinfo=timezone.utc),
        ]
    )
    monkeypatch.setattr(work_cmd, "_now", lambda: next(times))
    import_file = tmp_path / "task-imports.jsonl"
    import_file.write_text(
        json.dumps(
            {
                "text": "Promote and run scanner task",
                "kind": "task",
                "source": "repo-scan",
                "priority": "high",
                "acceptance": ["Promote run acceptance."],
            }
        )
        + "\n"
    )
    assert work_cmd.import_ingest(target=tmp_path, input_path=import_file) == 0
    item = json.loads((tmp_path / ".brigade" / "work" / "imports" / "inbox.jsonl").read_text().splitlines()[0])

    def fake_dogfood_run(task, **kwargs):
        run_dir = kwargs["output_dir"] or artifacts_dir / "promote-run"
        run_dir.mkdir(parents=True)
        _write_json(run_dir / "run.json", {"started_at": "2026-05-26T13:00:00Z", "status": "ok", "task": task})
        (run_dir / "final.txt").write_text("Done.\n\nNext step: Build follow-up.\n")
        return 0

    monkeypatch.setattr(dogfood_cmd, "run", fake_dogfood_run)

    assert work_cmd.import_promote(target=tmp_path, import_id=item["id"], run_after=True) == 0
    out = capsys.readouterr().out
    assert "run: starting" in out
    ledger = json.loads((tmp_path / ".brigade" / "work" / "tasks.json").read_text())
    task = ledger["tasks"][0]
    assert task["status"] == "done"
    assert task["completed_acceptance"] == ["Promote run acceptance."]
    assert task["completed_session_path"]
    assert json.loads((tmp_path / ".brigade" / "work" / "imports" / "inbox.jsonl").read_text())["status"] == "promoted"


def test_work_import_promote_run_failure_leaves_task_pending(tmp_path, monkeypatch, capsys):
    _init_git_repo(tmp_path)
    dogfood_cmd.init(target=tmp_path)
    times = iter(
        [
            datetime(2026, 5, 26, 12, 0, 0, tzinfo=timezone.utc),
            datetime(2026, 5, 26, 12, 1, 0, tzinfo=timezone.utc),
            datetime(2026, 5, 26, 12, 1, 1, tzinfo=timezone.utc),
            datetime(2026, 5, 26, 13, 0, 0, tzinfo=timezone.utc),
            datetime(2026, 5, 26, 13, 0, 1, tzinfo=timezone.utc),
        ]
    )
    monkeypatch.setattr(work_cmd, "_now", lambda: next(times))
    assert work_cmd.import_add(target=tmp_path, text="Promote run failure", kind="task", source="repo-scan") == 0
    import_id = capsys.readouterr().out.split("import: ", 1)[1].splitlines()[0]
    monkeypatch.setattr(dogfood_cmd, "run", lambda task, **kwargs: 7)

    assert work_cmd.import_promote(target=tmp_path, import_id=import_id, run_after=True) == 7
    ledger = json.loads((tmp_path / ".brigade" / "work" / "tasks.json").read_text())
    task = ledger["tasks"][0]
    assert task["status"] == "pending"
    assert "completed_at" not in task
    imports = json.loads((tmp_path / ".brigade" / "work" / "imports" / "inbox.jsonl").read_text())
    assert imports["status"] == "promoted"


def test_work_import_promote_all_filters_by_metadata(tmp_path, monkeypatch, capsys):
    _init_git_repo(tmp_path)
    monkeypatch.setattr(
        work_cmd,
        "_now",
        lambda: datetime(2026, 5, 26, 12, 0, 0, tzinfo=timezone.utc),
    )
    assert (
        work_cmd.import_add(
            target=tmp_path,
            text="Fix route skip",
            kind="task",
            source="handoff-ingest",
            metadata=["handoff_issue_category=route-skip"],
        )
        == 0
    )
    assert (
        work_cmd.import_add(
            target=tmp_path,
            text="Fix malformed handoff",
            kind="task",
            source="handoff-ingest",
            metadata=["handoff_issue_category=skip"],
        )
        == 0
    )
    capsys.readouterr()

    assert (
        work_cmd.import_promote(
            target=tmp_path,
            all_matching=True,
            source="handoff-ingest",
            metadata=["handoff_issue_category=route-skip"],
        )
        == 0
    )
    out = capsys.readouterr().out
    assert "promoted: 1" in out
    ledger = json.loads((tmp_path / ".brigade" / "work" / "tasks.json").read_text())
    assert [task["text"] for task in ledger["tasks"]] == ["Fix route skip"]
    assert work_cmd.import_list(target=tmp_path, json_output=True) == 0
    payload = json.loads(capsys.readouterr().out)
    assert [item["text"] for item in payload["imports"]] == ["Fix malformed handoff"]


def test_work_import_dismiss_marks_import_not_pending(tmp_path, monkeypatch, capsys):
    _init_git_repo(tmp_path)
    monkeypatch.setattr(
        work_cmd,
        "_now",
        lambda: datetime(2026, 5, 26, 12, 0, 0, tzinfo=timezone.utc),
    )
    assert work_cmd.import_add(target=tmp_path, text="Ignore noisy scanner item", source="discord") == 0
    import_id = capsys.readouterr().out.split("import: ", 1)[1].splitlines()[0]

    assert work_cmd.import_dismiss(target=tmp_path, import_id=import_id[:12], reason="not actionable") == 0
    out = capsys.readouterr().out
    assert "status: dismissed" in out
    assert "reason: not actionable" in out
    assert work_cmd.import_list(target=tmp_path) == 0
    assert "imports: none" in capsys.readouterr().out
    assert work_cmd.import_list(target=tmp_path, all_imports=True, json_output=True) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["imports"][0]["status"] == "dismissed"
    assert payload["imports"][0]["dismiss_reason"] == "not actionable"


def test_work_import_dismiss_all_filters_by_source_kind_and_metadata(tmp_path, monkeypatch, capsys):
    _init_git_repo(tmp_path)
    monkeypatch.setattr(
        work_cmd,
        "_now",
        lambda: datetime(2026, 5, 26, 12, 0, 0, tzinfo=timezone.utc),
    )
    assert (
        work_cmd.import_add(
            target=tmp_path,
            text="Dismiss skipped historical handoff",
            kind="task",
            source="handoff-ingest",
            metadata=["handoff_issue_category=skip"],
        )
        == 0
    )
    assert (
        work_cmd.import_add(
            target=tmp_path,
            text="Keep route skip",
            kind="task",
            source="handoff-ingest",
            metadata=["handoff_issue_category=route-skip"],
        )
        == 0
    )
    assert work_cmd.import_add(target=tmp_path, text="Keep incident", kind="incident", source="handoff-ingest") == 0
    capsys.readouterr()

    assert (
        work_cmd.import_dismiss(
            target=tmp_path,
            all_matching=True,
            kind="task",
            source="handoff-ingest",
            metadata=["handoff_issue_category=skip"],
            reason="historical noise",
        )
        == 0
    )
    out = capsys.readouterr().out
    assert "dismissed: 1" in out
    assert "reason: historical noise" in out

    assert work_cmd.import_list(target=tmp_path, json_output=True) == 0
    payload = json.loads(capsys.readouterr().out)
    assert [item["text"] for item in payload["imports"]] == ["Keep route skip", "Keep incident"]
    assert work_cmd.import_list(target=tmp_path, all_imports=True, json_output=True) == 0
    payload = json.loads(capsys.readouterr().out)
    dismissed = [item for item in payload["imports"] if item["text"] == "Dismiss skipped historical handoff"][0]
    assert dismissed["status"] == "dismissed"
    assert dismissed["dismiss_reason"] == "historical noise"


def test_work_import_dismiss_all_requires_id_or_all(tmp_path, capsys):
    _init_git_repo(tmp_path)

    assert work_cmd.import_dismiss(target=tmp_path) == 2
    assert "import id is required unless --all is passed" in capsys.readouterr().err


def test_work_import_promote_rejects_non_pending_import(tmp_path, monkeypatch, capsys):
    _init_git_repo(tmp_path)
    monkeypatch.setattr(
        work_cmd,
        "_now",
        lambda: datetime(2026, 5, 26, 12, 0, 0, tzinfo=timezone.utc),
    )
    assert work_cmd.import_add(target=tmp_path, text="Dismissed scanner item", source="discord") == 0
    import_id = capsys.readouterr().out.split("import: ", 1)[1].splitlines()[0]
    assert work_cmd.import_dismiss(target=tmp_path, import_id=import_id) == 0
    capsys.readouterr()

    assert work_cmd.import_promote(target=tmp_path, import_id=import_id) == 2
    assert "import is not pending" in capsys.readouterr().err

    imports = json.loads((tmp_path / ".brigade" / "work" / "imports" / "inbox.jsonl").read_text().splitlines()[0])
    assert imports["status"] == "dismissed"
    assert not (tmp_path / ".brigade" / "work" / "tasks.json").exists()


def test_work_import_dismiss_rejects_non_pending_import(tmp_path, monkeypatch, capsys):
    _init_git_repo(tmp_path)
    monkeypatch.setattr(
        work_cmd,
        "_now",
        lambda: datetime(2026, 5, 26, 12, 0, 0, tzinfo=timezone.utc),
    )
    assert work_cmd.import_add(target=tmp_path, text="Promote scanner item", source="slack") == 0
    import_id = capsys.readouterr().out.split("import: ", 1)[1].splitlines()[0]
    assert work_cmd.import_promote(target=tmp_path, import_id=import_id) == 0
    capsys.readouterr()

    assert work_cmd.import_dismiss(target=tmp_path, import_id=import_id, reason="late cleanup") == 2
    assert "import is not pending" in capsys.readouterr().err

    payload = json.loads((tmp_path / ".brigade" / "work" / "imports" / "inbox.jsonl").read_text().splitlines()[0])
    assert payload["status"] == "promoted"
    assert "dismiss_reason" not in payload


def test_work_brief_includes_pending_imports(tmp_path, monkeypatch, capsys):
    _init_git_repo(tmp_path)
    monkeypatch.setattr(
        work_cmd,
        "_now",
        lambda: datetime(2026, 5, 26, 12, 0, 0, tzinfo=timezone.utc),
    )
    assert (
        work_cmd.import_add(
            target=tmp_path,
            text="Review expired decision card",
            kind="finding",
            source="memory-care",
        )
        == 0
    )
    capsys.readouterr()

    assert work_cmd.brief(target=tmp_path, json_output=True) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["imports_path"].endswith(".brigade/work/imports/inbox.jsonl")
    assert payload["pending_imports"][0]["text"] == "Review expired decision card"
    assert payload["pending_imports"][0]["kind"] == "finding"
    assert payload["pending_imports"][0]["source"] == "memory-care"
    assert payload["pending_import_counts"]["total"] == 1
    assert payload["pending_import_counts"]["by_source"] == {"memory-care": 1}
    assert payload["pending_import_counts"]["by_kind"] == {"finding": 1}

    assert work_cmd.brief(target=tmp_path) == 0
    out = capsys.readouterr().out
    assert "pending_import_count: 1" in out
    assert "pending_imports_by_source:" in out
    assert "  memory-care: 1" in out
    assert "pending_imports_by_kind:" in out
    assert "  finding: 1" in out


def test_work_brief_includes_handoff_ingest_issues(tmp_path, monkeypatch, capsys):
    _init_git_repo(tmp_path)
    monkeypatch.setattr(work_cmd.shutil, "which", lambda name: f"/usr/bin/{name}")
    log = tmp_path / ".brigade" / "handoff-ingest" / "latest.log"
    log.parent.mkdir(parents=True)
    log.write_text("SKIP bad.md: no recognizable markdown sections found\n")
    config = tmp_path / ".brigade" / "handoff-sources.json"
    config.write_text(
        json.dumps(
            {
                "sources": [{"root": ".", "inboxes": [".claude/memory-handoffs"]}],
                "ingestor": {"last_run_log": ".brigade/handoff-ingest/latest.log"},
            }
        )
    )

    assert work_cmd.brief(target=tmp_path, json_output=True) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["handoff_issues"]["count"] == 1
    assert payload["handoff_issues"]["known_count"] == 0
    assert payload["handoff_issues"]["total_count"] == 1
    assert payload["handoff_issues"]["by_category"] == {"skip": 1}

    assert work_cmd.brief(target=tmp_path) == 0
    out = capsys.readouterr().out
    assert "handoff_ingest_issues_new: 1" in out
    assert "handoff_ingest_issues_by_category:" in out
    assert "  skip: 1" in out


def test_work_brief_suppresses_known_handoff_ingest_issues(tmp_path, monkeypatch, capsys):
    _init_git_repo(tmp_path)
    monkeypatch.setattr(work_cmd.shutil, "which", lambda name: f"/usr/bin/{name}")
    log = tmp_path / ".brigade" / "handoff-ingest" / "latest.log"
    log.parent.mkdir(parents=True)
    log.write_text("SKIP bad.md: no recognizable markdown sections found\n")
    config = tmp_path / ".brigade" / "handoff-sources.json"
    config.write_text(
        json.dumps(
            {
                "sources": [{"root": ".", "inboxes": [".claude/memory-handoffs"]}],
                "ingestor": {"last_run_log": ".brigade/handoff-ingest/latest.log"},
            }
        )
    )
    from brigade import handoff_cmd

    issue = handoff_cmd.collect_issues(tmp_path)[0]
    dismissed = work_cmd._make_import(
        issue.text,
        kind=issue.kind,
        source="handoff-ingest",
        metadata=issue.as_import_record()["metadata"],
    )
    dismissed["status"] = "dismissed"
    work_cmd._write_imports(tmp_path, [dismissed])

    assert work_cmd.brief(target=tmp_path, json_output=True) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["handoff_issues"]["count"] == 0
    assert payload["handoff_issues"]["known_count"] == 1
    assert payload["handoff_issues"]["known_by_category"] == {"skip": 1}

    assert work_cmd.brief(target=tmp_path) == 0
    out = capsys.readouterr().out
    assert "handoff_ingest_issues_new" not in out
    assert "handoff_ingest_issues_known: 1" in out


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


def test_work_run_consumes_pending_task_before_latest_next(tmp_path, monkeypatch):
    _init_git_repo(tmp_path)
    artifacts_dir = tmp_path / ".brigade" / "runs"
    dogfood_cmd.init(target=tmp_path, artifacts_dir=artifacts_dir)
    latest_dir = artifacts_dir / "latest"
    latest_dir.mkdir(parents=True)
    _write_json(
        latest_dir / "run.json",
        {"started_at": "2026-05-26T11:00:00Z", "status": "ok", "task": "review"},
    )
    (latest_dir / "final.txt").write_text("Done.\n\nNext step: Build extracted task.\n")
    times = iter(
        [
            datetime(2026, 5, 26, 11, 30, 0, tzinfo=timezone.utc),
            datetime(2026, 5, 26, 12, 0, 0, tzinfo=timezone.utc),
            datetime(2026, 5, 26, 13, 0, 0, tzinfo=timezone.utc),
            datetime(2026, 5, 26, 13, 0, 1, tzinfo=timezone.utc),
        ]
    )
    monkeypatch.setattr(work_cmd, "_now", lambda: next(times))
    assert work_cmd.task_add(target=tmp_path, text="Build queued task") == 0
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
    assert seen["task"] == "Build queued task"
    ledger = json.loads((tmp_path / ".brigade" / "work" / "tasks.json").read_text())
    assert ledger["tasks"][0]["status"] == "done"
    assert ledger["tasks"][0]["completed_session_title"] == "Build queued task"


def test_work_run_records_task_snapshot_and_completion_metadata(tmp_path, monkeypatch, capsys):
    _init_git_repo(tmp_path)
    artifacts_dir = tmp_path / ".brigade" / "runs"
    dogfood_cmd.init(target=tmp_path, artifacts_dir=artifacts_dir)
    times = iter(
        [
            datetime(2026, 5, 26, 12, 0, 0, tzinfo=timezone.utc),
            datetime(2026, 5, 26, 13, 0, 0, tzinfo=timezone.utc),
            datetime(2026, 5, 26, 13, 0, 1, tzinfo=timezone.utc),
        ]
    )
    monkeypatch.setattr(work_cmd, "_now", lambda: next(times))
    work_cmd._write_task_ledger(
        tmp_path,
        {
            "version": 1,
            "tasks": [
                {
                    "id": "issue-task",
                    "text": "Build acceptance evidence",
                    "status": "pending",
                    "source": "github_issue",
                    "type": "feature",
                    "priority": "high",
                    "template": "vertical-slice",
                    "acceptance": ["Session records the acceptance checklist."],
                    "created_at": "2026-05-26T11:30:00+00:00",
                    "updated_at": "2026-05-26T11:30:00+00:00",
                    "metadata": {
                        "github_issue": {
                            "url": "https://github.com/acme/widgets/issues/45",
                            "number": 45,
                            "title": "Build acceptance evidence",
                            "labels": ["tdd"],
                            "state": "OPEN",
                            "source": "gh",
                            "ref": "45",
                        }
                    },
                }
            ],
        },
    )

    def fake_dogfood_run(task, **kwargs):
        run_dir = kwargs["output_dir"]
        run_dir.mkdir(parents=True)
        _write_json(run_dir / "run.json", {"started_at": "2026-05-26T12:10:00Z", "status": "ok", "task": task})
        (run_dir / "final.txt").write_text("Done.\n\nNext step: Build follow-up.\n")
        return 0

    monkeypatch.setattr(dogfood_cmd, "run", fake_dogfood_run)
    run_dir = artifacts_dir / "new"

    assert work_cmd.run(None, target=tmp_path, output_dir=run_dir, handoff=False) == 0
    capsys.readouterr()

    session_dir = tmp_path / ".brigade" / "work" / "20260526-120000-build-acceptance-evidence"
    payload = json.loads((session_dir / "session.json").read_text())
    assert payload["task"] == {
        "id": "issue-task",
        "text": "Build acceptance evidence",
        "source": "github_issue",
        "type": "feature",
        "priority": "high",
        "acceptance": ["Session records the acceptance checklist."],
        "acceptance_count": 1,
        "template": "vertical-slice",
        "issue": {
            "url": "https://github.com/acme/widgets/issues/45",
            "number": 45,
            "title": "Build acceptance evidence",
            "labels": ["tdd"],
            "state": "OPEN",
            "source": "gh",
            "ref": "45",
        },
    }
    start_md = (session_dir / "start.md").read_text()
    end_md = (session_dir / "end.md").read_text()
    for rendered in (start_md, end_md):
        assert "## Task" in rendered
        assert "- Task: `issue-task`" in rendered
        assert "- Source: github_issue" in rendered
        assert "- Type: feature" in rendered
        assert "- Priority: high" in rendered
        assert "- Template: vertical-slice" in rendered
        assert "- Issue: https://github.com/acme/widgets/issues/45" in rendered
        assert "### Acceptance Criteria" in rendered
        assert "- Session records the acceptance checklist." in rendered

    ledger = json.loads((tmp_path / ".brigade" / "work" / "tasks.json").read_text())
    task = ledger["tasks"][0]
    assert task["status"] == "done"
    assert task["completed_session_path"] == str(session_dir)
    assert task["completed_run_path"] == str(run_dir)
    assert task["completed_acceptance"] == ["Session records the acceptance checklist."]

    assert work_cmd.task_show(target=tmp_path, task_id="issue-task") == 0
    out = capsys.readouterr().out
    assert f"completed_session_path: {session_dir}" in out
    assert f"completed_run_path: {run_dir}" in out
    assert "completed_acceptance: 1" in out
    assert "Session records the acceptance checklist." in out


def test_work_run_passes_acceptance_criteria_for_pending_task(tmp_path, monkeypatch):
    _init_git_repo(tmp_path)
    artifacts_dir = tmp_path / ".brigade" / "runs"
    dogfood_cmd.init(target=tmp_path, artifacts_dir=artifacts_dir)
    times = iter(
        [
            datetime(2026, 5, 26, 11, 30, 0, tzinfo=timezone.utc),
            datetime(2026, 5, 26, 12, 0, 0, tzinfo=timezone.utc),
            datetime(2026, 5, 26, 13, 0, 0, tzinfo=timezone.utc),
            datetime(2026, 5, 26, 13, 0, 1, tzinfo=timezone.utc),
        ]
    )
    monkeypatch.setattr(work_cmd, "_now", lambda: next(times))
    assert (
        work_cmd.task_add(
            target=tmp_path,
            text="Build accepted queue",
            task_type="feature",
            priority="high",
            acceptance=["Dogfood prompt includes this criterion"],
        )
        == 0
    )
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
    assert seen["task"].startswith("Build accepted queue")
    assert "Acceptance criteria:" in seen["task"]
    assert "- Dogfood prompt includes this criterion" in seen["task"]
    assert "- type: feature" in seen["task"]
    assert "- priority: high" in seen["task"]
    ledger = json.loads((tmp_path / ".brigade" / "work" / "tasks.json").read_text())
    assert ledger["tasks"][0]["status"] == "done"
    assert ledger["tasks"][0]["completed_session_title"] == "Build accepted queue"


def test_work_run_queue_next_adds_extracted_followup(tmp_path, monkeypatch, capsys):
    _init_git_repo(tmp_path)
    artifacts_dir = tmp_path / ".brigade" / "runs"
    dogfood_cmd.init(target=tmp_path, artifacts_dir=artifacts_dir)
    times = iter(
        [
            datetime(2026, 5, 26, 12, 0, 0, tzinfo=timezone.utc),
            datetime(2026, 5, 26, 13, 0, 0, tzinfo=timezone.utc),
            datetime(2026, 5, 26, 13, 0, 1, tzinfo=timezone.utc),
        ]
    )
    monkeypatch.setattr(work_cmd, "_now", lambda: next(times))

    def fake_dogfood_run(task, **kwargs):
        run_dir = kwargs["output_dir"]
        run_dir.mkdir(parents=True)
        _write_json(
            run_dir / "run.json",
            {"started_at": "2026-05-26T12:10:00Z", "status": "ok", "task": task},
        )
        (run_dir / "final.txt").write_text("Done.\n\nNext step: Build queued follow-up.\n")
        return 0

    monkeypatch.setattr(dogfood_cmd, "run", fake_dogfood_run)

    assert (
        work_cmd.run(
            "review the repo",
            target=tmp_path,
            output_dir=artifacts_dir / "new",
            handoff=False,
            queue_next=True,
        )
        == 0
    )
    out = capsys.readouterr().out
    assert "queued_next:" in out
    assert "(created)" in out
    ledger = json.loads((tmp_path / ".brigade" / "work" / "tasks.json").read_text())
    assert ledger["tasks"][0]["status"] == "pending"
    assert ledger["tasks"][0]["text"] == "Build queued follow-up."
    assert ledger["tasks"][0]["source"] == "latest_dogfood_run"
    assert ledger["tasks"][0]["metadata"]["run_path"] == str(artifacts_dir / "new")
    assert ledger["tasks"][0]["metadata"]["session_title"] == "review the repo"


def test_work_run_queue_next_reuses_existing_pending_task(tmp_path, monkeypatch, capsys):
    _init_git_repo(tmp_path)
    artifacts_dir = tmp_path / ".brigade" / "runs"
    dogfood_cmd.init(target=tmp_path, artifacts_dir=artifacts_dir)
    times = iter(
        [
            datetime(2026, 5, 26, 11, 30, 0, tzinfo=timezone.utc),
            datetime(2026, 5, 26, 12, 0, 0, tzinfo=timezone.utc),
            datetime(2026, 5, 26, 13, 0, 0, tzinfo=timezone.utc),
        ]
    )
    monkeypatch.setattr(work_cmd, "_now", lambda: next(times))
    assert work_cmd.task_add(target=tmp_path, text="Build queued follow-up.") == 0
    capsys.readouterr()

    def fake_dogfood_run(task, **kwargs):
        run_dir = kwargs["output_dir"]
        run_dir.mkdir(parents=True)
        _write_json(
            run_dir / "run.json",
            {"started_at": "2026-05-26T12:10:00Z", "status": "ok", "task": task},
        )
        (run_dir / "final.txt").write_text("Done.\n\nNext step: Build queued follow-up.\n")
        return 0

    monkeypatch.setattr(dogfood_cmd, "run", fake_dogfood_run)

    assert (
        work_cmd.run(
            "review the repo",
            target=tmp_path,
            output_dir=artifacts_dir / "new",
            handoff=False,
            queue_next=True,
        )
        == 0
    )
    out = capsys.readouterr().out
    assert "(existing)" in out
    ledger = json.loads((tmp_path / ".brigade" / "work" / "tasks.json").read_text())
    assert len(ledger["tasks"]) == 1


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


def test_work_run_leaves_consumed_task_pending_when_dogfood_fails(tmp_path, monkeypatch):
    _init_git_repo(tmp_path)
    dogfood_cmd.init(target=tmp_path)
    times = iter(
        [
            datetime(2026, 5, 26, 12, 0, 0, tzinfo=timezone.utc),
            datetime(2026, 5, 26, 13, 0, 0, tzinfo=timezone.utc),
            datetime(2026, 5, 26, 13, 0, 1, tzinfo=timezone.utc),
        ]
    )
    monkeypatch.setattr(work_cmd, "_now", lambda: next(times))
    assert work_cmd.task_add(target=tmp_path, text="Build pending failure", acceptance=["Do not complete on failure"]) == 0
    monkeypatch.setattr(dogfood_cmd, "run", lambda task, **kwargs: 7)

    assert work_cmd.run(None, target=tmp_path, handoff=False) == 7
    ledger = json.loads((tmp_path / ".brigade" / "work" / "tasks.json").read_text())
    task = ledger["tasks"][0]
    assert task["status"] == "pending"
    assert "completed_at" not in task
    assert "completed_session_path" not in task
    session_dir = tmp_path / ".brigade" / "work" / "20260526-130000-build-pending-failure"
    payload = json.loads((session_dir / "session.json").read_text())
    assert payload["task"]["id"] == task["id"]


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


def test_work_brief_cli(tmp_path, monkeypatch):
    seen = {}

    def fake_brief(**kwargs):
        seen.update(kwargs)
        return 0

    monkeypatch.setattr(work_cmd, "brief", fake_brief)

    assert cli.main(["work", "brief", "--target", str(tmp_path), "--limit", "4"]) == 0
    assert seen == {"target": tmp_path, "limit": 4, "json_output": False}
    seen.clear()
    assert cli.main(["work", "brief", "--target", str(tmp_path), "--json"]) == 0
    assert seen == {"target": tmp_path, "limit": 3, "json_output": True}


def test_work_inbox_cli(tmp_path, monkeypatch):
    seen = []

    def fake_inbox(**kwargs):
        seen.append(("inbox", kwargs))
        return 0

    def fake_inbox_doctor(**kwargs):
        seen.append(("doctor", kwargs))
        return 0

    def fake_inbox_archive(**kwargs):
        seen.append(("archive", kwargs))
        return 0

    monkeypatch.setattr(work_cmd, "inbox", fake_inbox)
    monkeypatch.setattr(work_cmd, "inbox_doctor", fake_inbox_doctor)
    monkeypatch.setattr(work_cmd, "inbox_archive", fake_inbox_archive)

    assert cli.main(["work", "inbox", "--target", str(tmp_path), "--limit", "7"]) == 0
    assert cli.main(["work", "inbox", "--target", str(tmp_path), "--json"]) == 0
    assert cli.main(["work", "inbox", "doctor", "--target", str(tmp_path), "--json"]) == 0
    assert cli.main(["work", "inbox", "archive", "--target", str(tmp_path), "--json"]) == 0
    assert seen == [
        ("inbox", {"target": tmp_path, "json_output": False, "limit": 7}),
        ("inbox", {"target": tmp_path, "json_output": True, "limit": 20}),
        ("doctor", {"target": tmp_path, "json_output": True}),
        ("archive", {"target": tmp_path, "json_output": True}),
    ]


def test_work_scanners_cli(tmp_path, monkeypatch):
    seen = []

    def fake_scanners_init(**kwargs):
        seen.append(("init", kwargs))
        return 0

    def fake_scanners_list(**kwargs):
        seen.append(("list", kwargs))
        return 0

    def fake_scanners_show(**kwargs):
        seen.append(("show", kwargs))
        return 0

    def fake_scanners_plan(**kwargs):
        seen.append(("plan", kwargs))
        return 0

    def fake_scanners_doctor(**kwargs):
        seen.append(("doctor", kwargs))
        return 0

    def fake_scanners_run(**kwargs):
        seen.append(("run", kwargs))
        return 0

    def fake_scanners_runs(**kwargs):
        seen.append(("runs", kwargs))
        return 0

    def fake_scanners_run_show(**kwargs):
        seen.append(("run-show", kwargs))
        return 0

    monkeypatch.setattr(work_cmd, "scanners_init", fake_scanners_init)
    monkeypatch.setattr(work_cmd, "scanners_list", fake_scanners_list)
    monkeypatch.setattr(work_cmd, "scanners_show", fake_scanners_show)
    monkeypatch.setattr(work_cmd, "scanners_plan", fake_scanners_plan)
    monkeypatch.setattr(work_cmd, "scanners_doctor", fake_scanners_doctor)
    monkeypatch.setattr(work_cmd, "scanners_run", fake_scanners_run)
    monkeypatch.setattr(work_cmd, "scanners_runs", fake_scanners_runs)
    monkeypatch.setattr(work_cmd, "scanners_run_show", fake_scanners_run_show)

    assert cli.main(["work", "scanners", "init", "--target", str(tmp_path), "--force", "--no-gitignore"]) == 0
    assert cli.main(["work", "scanners", "list", "--target", str(tmp_path), "--json"]) == 0
    assert cli.main(["work", "scanners", "show", "chat-memory-sweep", "--target", str(tmp_path), "--json"]) == 0
    assert cli.main(["work", "scanners", "plan", "--target", str(tmp_path), "--json"]) == 0
    assert cli.main(["work", "scanners", "doctor", "--target", str(tmp_path), "--json", "--import-issues"]) == 0
    assert (
        cli.main(
            [
                "work",
                "scanners",
                "run",
                "chat-memory-sweep",
                "--target",
                str(tmp_path),
                "--include-disabled",
                "--force",
                "--ingest-output",
                "--json",
            ]
        )
        == 0
    )
    assert cli.main(["work", "scanners", "run", "--due", "--target", str(tmp_path)]) == 0
    assert cli.main(["work", "scanners", "runs", "--target", str(tmp_path), "--limit", "5", "--json"]) == 0
    assert cli.main(["work", "scanners", "run-show", "run-1", "--target", str(tmp_path), "--json"]) == 0
    assert seen == [
        ("init", {"target": tmp_path, "force": True, "update_gitignore": False}),
        ("list", {"target": tmp_path, "json_output": True}),
        ("show", {"target": tmp_path, "scanner_id": "chat-memory-sweep", "json_output": True}),
        ("plan", {"target": tmp_path, "json_output": True}),
        ("doctor", {"target": tmp_path, "json_output": True, "import_issues": True}),
        (
            "run",
            {
                "target": tmp_path,
                "scanner_id": "chat-memory-sweep",
                "all_matching": False,
                "due": False,
                "include_disabled": True,
                "force": True,
                "ingest_output": True,
                "json_output": True,
            },
        ),
        (
            "run",
            {
                "target": tmp_path,
                "scanner_id": None,
                "all_matching": False,
                "due": True,
                "include_disabled": False,
                "force": False,
                "ingest_output": False,
                "json_output": False,
            },
        ),
        ("runs", {"target": tmp_path, "json_output": True, "limit": 5}),
        ("run-show", {"target": tmp_path, "run_id": "run-1", "json_output": True}),
    ]


def test_work_sweep_cli(tmp_path, monkeypatch):
    seen = []

    def fake_sweep(**kwargs):
        seen.append(("sweep", kwargs))
        return 0

    def fake_sweeps(**kwargs):
        seen.append(("sweeps", kwargs))
        return 0

    def fake_sweep_show(**kwargs):
        seen.append(("sweep-show", kwargs))
        return 0

    def fake_sweep_review(**kwargs):
        seen.append(("sweep-review", kwargs))
        return 0

    monkeypatch.setattr(work_cmd, "sweep", fake_sweep)
    monkeypatch.setattr(work_cmd, "sweeps", fake_sweeps)
    monkeypatch.setattr(work_cmd, "sweep_show", fake_sweep_show)
    monkeypatch.setattr(work_cmd, "sweep_review", fake_sweep_review)

    assert (
        cli.main(
            [
                "work",
                "sweep",
                "--target",
                str(tmp_path),
                "--scanner",
                "repo-scan",
                "--include-disabled",
                "--force",
                "--no-ingest",
                "--json",
            ]
        )
        == 0
    )
    assert cli.main(["work", "sweep", "--target", str(tmp_path), "--all"]) == 0
    assert cli.main(["work", "sweeps", "--target", str(tmp_path), "--limit", "5", "--json"]) == 0
    assert cli.main(["work", "sweep-show", "sweep-1", "--target", str(tmp_path), "--json"]) == 0
    assert cli.main(["work", "sweep-review", "latest", "--target", str(tmp_path), "--json"]) == 0
    assert seen == [
        (
            "sweep",
            {
                "target": tmp_path,
                "scanner_id": "repo-scan",
                "all_matching": False,
                "include_disabled": True,
                "force": True,
                "ingest": False,
                "json_output": True,
            },
        ),
        (
            "sweep",
            {
                "target": tmp_path,
                "scanner_id": None,
                "all_matching": True,
                "include_disabled": False,
                "force": False,
                "ingest": True,
                "json_output": False,
            },
        ),
        ("sweeps", {"target": tmp_path, "limit": 5, "json_output": True}),
        ("sweep-show", {"target": tmp_path, "sweep_id": "sweep-1", "json_output": True}),
        ("sweep-review", {"target": tmp_path, "sweep_id": "latest", "json_output": True}),
    ]


def test_work_backup_cli(tmp_path, monkeypatch):
    seen = []

    def fake_backup_init(**kwargs):
        seen.append(("init", kwargs))
        return 0

    def fake_backup_status(**kwargs):
        seen.append(("status", kwargs))
        return 0

    def fake_backup_doctor(**kwargs):
        seen.append(("doctor", kwargs))
        return 0

    def fake_backup_import_issues(**kwargs):
        seen.append(("import-issues", kwargs))
        return 0

    monkeypatch.setattr(work_cmd, "backup_init", fake_backup_init)
    monkeypatch.setattr(work_cmd, "backup_status", fake_backup_status)
    monkeypatch.setattr(work_cmd, "backup_doctor", fake_backup_doctor)
    monkeypatch.setattr(work_cmd, "backup_import_issues", fake_backup_import_issues)

    assert cli.main(["work", "backup", "init", "--target", str(tmp_path), "--force", "--no-gitignore"]) == 0
    assert cli.main(["work", "backup", "status", "--target", str(tmp_path), "--json"]) == 0
    assert cli.main(["work", "backup", "doctor", "--target", str(tmp_path), "--json"]) == 0
    assert cli.main(["work", "backup", "import-issues", "--target", str(tmp_path), "--json"]) == 0
    assert seen == [
        ("init", {"target": tmp_path, "force": True, "update_gitignore": False}),
        ("status", {"target": tmp_path, "json_output": True}),
        ("doctor", {"target": tmp_path, "json_output": True}),
        ("import-issues", {"target": tmp_path, "json_output": True}),
    ]


def test_tools_cli(tmp_path, monkeypatch):
    seen = []

    def fake_init(**kwargs):
        seen.append(("init", kwargs))
        return 0

    def fake_list(**kwargs):
        seen.append(("list", kwargs))
        return 0

    def fake_show(**kwargs):
        seen.append(("show", kwargs))
        return 0

    def fake_search(**kwargs):
        seen.append(("search", kwargs))
        return 0

    def fake_describe(**kwargs):
        seen.append(("describe", kwargs))
        return 0

    def fake_contracts(**kwargs):
        seen.append(("contracts", kwargs))
        return 0

    def fake_call_plan(**kwargs):
        seen.append(("call-plan", kwargs))
        return 0

    def fake_call_queue(**kwargs):
        seen.append(("call-queue", kwargs))
        return 0

    def fake_call_list(**kwargs):
        seen.append(("call-list", kwargs))
        return 0

    def fake_call_show(**kwargs):
        seen.append(("call-show", kwargs))
        return 0

    def fake_call_approve(**kwargs):
        seen.append(("call-approve", kwargs))
        return 0

    def fake_call_reject(**kwargs):
        seen.append(("call-reject", kwargs))
        return 0

    def fake_call_hold(**kwargs):
        seen.append(("call-hold", kwargs))
        return 0

    def fake_call_run(**kwargs):
        seen.append(("call-run", kwargs))
        return 0

    def fake_run_list(**kwargs):
        seen.append(("run-list", kwargs))
        return 0

    def fake_run_show(**kwargs):
        seen.append(("run-show", kwargs))
        return 0

    def fake_run_latest(**kwargs):
        seen.append(("run-latest", kwargs))
        return 0

    def fake_run_replay(**kwargs):
        seen.append(("run-replay", kwargs))
        return 0

    def fake_checkpoint_list(**kwargs):
        seen.append(("checkpoint-list", kwargs))
        return 0

    def fake_checkpoint_show(**kwargs):
        seen.append(("checkpoint-show", kwargs))
        return 0

    def fake_checkpoint_approve(**kwargs):
        seen.append(("checkpoint-approve", kwargs))
        return 0

    def fake_checkpoint_reject(**kwargs):
        seen.append(("checkpoint-reject", kwargs))
        return 0

    def fake_checkpoint_resume(**kwargs):
        seen.append(("checkpoint-resume", kwargs))
        return 0

    def fake_runtime_init(**kwargs):
        seen.append(("runtime-init", kwargs))
        return 0

    def fake_runtime_list(**kwargs):
        seen.append(("runtime-list", kwargs))
        return 0

    def fake_runtime_show(**kwargs):
        seen.append(("runtime-show", kwargs))
        return 0

    def fake_runtime_status(**kwargs):
        seen.append(("runtime-status", kwargs))
        return 0

    def fake_runtime_start(**kwargs):
        seen.append(("runtime-start", kwargs))
        return 0

    def fake_runtime_stop(**kwargs):
        seen.append(("runtime-stop", kwargs))
        return 0

    def fake_runtime_restart(**kwargs):
        seen.append(("runtime-restart", kwargs))
        return 0

    def fake_runtime_doctor(**kwargs):
        seen.append(("runtime-doctor", kwargs))
        return 0

    def fake_policy_init(**kwargs):
        seen.append(("policy-init", kwargs))
        return 0

    def fake_policy_show(**kwargs):
        seen.append(("policy-show", kwargs))
        return 0

    def fake_policy_doctor(**kwargs):
        seen.append(("policy-doctor", kwargs))
        return 0

    def fake_plan(**kwargs):
        seen.append(("plan", kwargs))
        return 0

    def fake_apply(**kwargs):
        seen.append(("apply", kwargs))
        return 0

    def fake_doctor(**kwargs):
        seen.append(("doctor", kwargs))
        return 0

    def fake_import_issues(**kwargs):
        seen.append(("import-issues", kwargs))
        return 0

    monkeypatch.setattr(tools_cmd, "init", fake_init)
    monkeypatch.setattr(tools_cmd, "list_tools", fake_list)
    monkeypatch.setattr(tools_cmd, "show", fake_show)
    monkeypatch.setattr(tools_cmd, "search", fake_search)
    monkeypatch.setattr(tools_cmd, "describe", fake_describe)
    monkeypatch.setattr(tools_cmd, "contracts", fake_contracts)
    monkeypatch.setattr(tools_cmd, "call_plan", fake_call_plan)
    monkeypatch.setattr(tools_cmd, "call_queue", fake_call_queue)
    monkeypatch.setattr(tools_cmd, "call_list", fake_call_list)
    monkeypatch.setattr(tools_cmd, "call_show", fake_call_show)
    monkeypatch.setattr(tools_cmd, "call_approve", fake_call_approve)
    monkeypatch.setattr(tools_cmd, "call_reject", fake_call_reject)
    monkeypatch.setattr(tools_cmd, "call_hold", fake_call_hold)
    monkeypatch.setattr(tools_cmd, "call_run", fake_call_run)
    monkeypatch.setattr(tools_cmd, "run_list", fake_run_list)
    monkeypatch.setattr(tools_cmd, "run_show", fake_run_show)
    monkeypatch.setattr(tools_cmd, "run_latest", fake_run_latest)
    monkeypatch.setattr(tools_cmd, "run_replay", fake_run_replay)
    monkeypatch.setattr(tools_cmd, "checkpoint_list", fake_checkpoint_list)
    monkeypatch.setattr(tools_cmd, "checkpoint_show", fake_checkpoint_show)
    monkeypatch.setattr(tools_cmd, "checkpoint_approve", fake_checkpoint_approve)
    monkeypatch.setattr(tools_cmd, "checkpoint_reject", fake_checkpoint_reject)
    monkeypatch.setattr(tools_cmd, "checkpoint_resume", fake_checkpoint_resume)
    monkeypatch.setattr(tools_cmd, "runtime_init", fake_runtime_init)
    monkeypatch.setattr(tools_cmd, "runtime_list", fake_runtime_list)
    monkeypatch.setattr(tools_cmd, "runtime_show", fake_runtime_show)
    monkeypatch.setattr(tools_cmd, "runtime_status", fake_runtime_status)
    monkeypatch.setattr(tools_cmd, "runtime_start", fake_runtime_start)
    monkeypatch.setattr(tools_cmd, "runtime_stop", fake_runtime_stop)
    monkeypatch.setattr(tools_cmd, "runtime_restart", fake_runtime_restart)
    monkeypatch.setattr(tools_cmd, "runtime_doctor", fake_runtime_doctor)
    monkeypatch.setattr(tools_cmd, "policy_init", fake_policy_init)
    monkeypatch.setattr(tools_cmd, "policy_show", fake_policy_show)
    monkeypatch.setattr(tools_cmd, "policy_doctor", fake_policy_doctor)
    monkeypatch.setattr(tools_cmd, "plan", fake_plan)
    monkeypatch.setattr(tools_cmd, "apply", fake_apply)
    monkeypatch.setattr(tools_cmd, "doctor", fake_doctor)
    monkeypatch.setattr(tools_cmd, "import_issues", fake_import_issues)

    assert cli.main(["tools", "init", "--target", str(tmp_path), "--force", "--no-gitignore"]) == 0
    assert cli.main(["tools", "list", "--target", str(tmp_path), "--json"]) == 0
    assert cli.main(["tools", "show", "simplify", "--target", str(tmp_path), "--json"]) == 0
    assert cli.main(["tools", "describe", "simplify", "--target", str(tmp_path), "--json"]) == 0
    assert cli.main(["tools", "contracts", "--target", str(tmp_path), "--json"]) == 0
    assert cli.main(["tools", "search", "simple", "--target", str(tmp_path), "--json"]) == 0
    assert cli.main(["tools", "call", "plan", "simplify", "--target", str(tmp_path), "--args", '{"x":1}', "--json"]) == 0
    assert cli.main(["tools", "call", "queue", "simplify", "--target", str(tmp_path), "--args", '{"x":1}', "--include-blocked", "--json"]) == 0
    assert cli.main(["tools", "call", "list", "--target", str(tmp_path), "--json"]) == 0
    assert cli.main(["tools", "call", "show", "call-123", "--target", str(tmp_path), "--json"]) == 0
    assert cli.main(["tools", "call", "approve", "call-123", "--target", str(tmp_path), "--json"]) == 0
    assert cli.main(["tools", "call", "reject", "call-123", "--target", str(tmp_path), "--reason", "no", "--json"]) == 0
    assert cli.main(["tools", "call", "hold", "call-123", "--target", str(tmp_path), "--reason", "wait", "--json"]) == 0
    assert cli.main(["tools", "call", "run", "call-123", "--target", str(tmp_path), "--json"]) == 0
    assert cli.main(["tools", "call", "run", "--next", "--target", str(tmp_path), "--json"]) == 0
    assert cli.main(["tools", "run", "list", "--target", str(tmp_path), "--json"]) == 0
    assert cli.main(["tools", "run", "show", "run-123", "--target", str(tmp_path), "--json"]) == 0
    assert cli.main(["tools", "run", "latest", "--target", str(tmp_path), "--json"]) == 0
    assert cli.main(["tools", "run", "replay", "run-123", "--target", str(tmp_path), "--json"]) == 0
    assert cli.main(["tools", "checkpoint", "list", "--target", str(tmp_path), "--json"]) == 0
    assert cli.main(["tools", "checkpoint", "show", "checkpoint-123", "--target", str(tmp_path), "--json"]) == 0
    assert cli.main(["tools", "checkpoint", "approve", "checkpoint-123", "--choice", "continue", "--target", str(tmp_path), "--json"]) == 0
    assert cli.main(["tools", "checkpoint", "reject", "checkpoint-123", "--reason", "no", "--target", str(tmp_path), "--json"]) == 0
    assert cli.main(["tools", "checkpoint", "resume", "checkpoint-123", "--target", str(tmp_path), "--json"]) == 0
    assert cli.main(["tools", "runtime", "init", "--target", str(tmp_path), "--force"]) == 0
    assert cli.main(["tools", "runtime", "list", "--target", str(tmp_path), "--json"]) == 0
    assert cli.main(["tools", "runtime", "show", "helper", "--target", str(tmp_path), "--json"]) == 0
    assert cli.main(["tools", "runtime", "status", "--target", str(tmp_path), "--json"]) == 0
    assert cli.main(["tools", "runtime", "start", "helper", "--target", str(tmp_path), "--json"]) == 0
    assert cli.main(["tools", "runtime", "stop", "helper", "--target", str(tmp_path), "--json"]) == 0
    assert cli.main(["tools", "runtime", "restart", "helper", "--target", str(tmp_path), "--json"]) == 0
    assert cli.main(["tools", "runtime", "doctor", "--target", str(tmp_path), "--json"]) == 0
    assert cli.main(["tools", "policy", "init", "--target", str(tmp_path), "--force"]) == 0
    assert cli.main(["tools", "policy", "show", "--target", str(tmp_path), "--json"]) == 0
    assert cli.main(["tools", "policy", "doctor", "--target", str(tmp_path), "--json"]) == 0
    assert cli.main(["tools", "plan", "simplify", "--target", str(tmp_path), "--json"]) == 0
    assert cli.main(["tools", "apply", "simplify", "--target", str(tmp_path), "--dry-run", "--force", "--json"]) == 0
    assert cli.main(["tools", "apply", "--all", "--target", str(tmp_path), "--json"]) == 0
    assert cli.main(["tools", "doctor", "--target", str(tmp_path), "--json"]) == 0
    assert cli.main(["tools", "import-issues", "--target", str(tmp_path), "--json"]) == 0
    assert seen == [
        ("init", {"target": tmp_path, "force": True, "update_gitignore": False}),
        ("list", {"target": tmp_path, "json_output": True}),
        ("show", {"target": tmp_path, "tool_id": "simplify", "json_output": True}),
        ("describe", {"target": tmp_path, "tool_id": "simplify", "json_output": True}),
        ("contracts", {"target": tmp_path, "json_output": True}),
        ("search", {"target": tmp_path, "query": "simple", "json_output": True}),
        (
            "call-plan",
            {
                "target": tmp_path,
                "tool_id": "simplify",
                "args": '{"x":1}',
                "args_json": None,
                "json_output": True,
            },
        ),
        (
            "call-queue",
            {
                "target": tmp_path,
                "tool_id": "simplify",
                "args": '{"x":1}',
                "args_json": None,
                "include_blocked": True,
                "json_output": True,
            },
        ),
        ("call-list", {"target": tmp_path, "json_output": True}),
        ("call-show", {"target": tmp_path, "call_id": "call-123", "json_output": True}),
        ("call-approve", {"target": tmp_path, "call_id": "call-123", "json_output": True}),
        ("call-reject", {"target": tmp_path, "call_id": "call-123", "reason": "no", "json_output": True}),
        ("call-hold", {"target": tmp_path, "call_id": "call-123", "reason": "wait", "json_output": True}),
        ("call-run", {"target": tmp_path, "call_id": "call-123", "next_call": False, "json_output": True}),
        ("call-run", {"target": tmp_path, "call_id": None, "next_call": True, "json_output": True}),
        ("run-list", {"target": tmp_path, "json_output": True}),
        ("run-show", {"target": tmp_path, "run_id": "run-123", "json_output": True}),
        ("run-latest", {"target": tmp_path, "json_output": True}),
        ("run-replay", {"target": tmp_path, "run_id": "run-123", "json_output": True}),
        ("checkpoint-list", {"target": tmp_path, "json_output": True}),
        ("checkpoint-show", {"target": tmp_path, "checkpoint_id": "checkpoint-123", "json_output": True}),
        ("checkpoint-approve", {"target": tmp_path, "checkpoint_id": "checkpoint-123", "choice": "continue", "json_output": True}),
        ("checkpoint-reject", {"target": tmp_path, "checkpoint_id": "checkpoint-123", "reason": "no", "json_output": True}),
        ("checkpoint-resume", {"target": tmp_path, "checkpoint_id": "checkpoint-123", "json_output": True}),
        ("runtime-init", {"target": tmp_path, "force": True}),
        ("runtime-list", {"target": tmp_path, "json_output": True}),
        ("runtime-show", {"target": tmp_path, "runtime_id": "helper", "json_output": True}),
        ("runtime-status", {"target": tmp_path, "json_output": True}),
        ("runtime-start", {"target": tmp_path, "runtime_id": "helper", "json_output": True}),
        ("runtime-stop", {"target": tmp_path, "runtime_id": "helper", "json_output": True}),
        ("runtime-restart", {"target": tmp_path, "runtime_id": "helper", "json_output": True}),
        ("runtime-doctor", {"target": tmp_path, "json_output": True}),
        ("policy-init", {"target": tmp_path, "force": True}),
        ("policy-show", {"target": tmp_path, "json_output": True}),
        ("policy-doctor", {"target": tmp_path, "json_output": True}),
        ("plan", {"target": tmp_path, "tool_id": "simplify", "json_output": True}),
        (
            "apply",
            {
                "target": tmp_path,
                "tool_id": "simplify",
                "all_tools": False,
                "dry_run": True,
                "force": True,
                "json_output": True,
            },
        ),
        (
            "apply",
            {
                "target": tmp_path,
                "tool_id": None,
                "all_tools": True,
                "dry_run": False,
                "force": False,
                "json_output": True,
            },
        ),
        ("doctor", {"target": tmp_path, "json_output": True}),
        ("import-issues", {"target": tmp_path, "json_output": True}),
    ]


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


def test_work_tasks_cli(tmp_path, monkeypatch):
    seen = []

    def fake_tasks(**kwargs):
        seen.append(("tasks", kwargs))
        return 0

    def fake_task_add(**kwargs):
        seen.append(("add", kwargs))
        return 0

    def fake_task_show(**kwargs):
        seen.append(("show", kwargs))
        return 0

    def fake_task_plan(**kwargs):
        seen.append(("plan", kwargs))
        return 0

    def fake_task_done(**kwargs):
        seen.append(("done", kwargs))
        return 0

    monkeypatch.setattr(work_cmd, "tasks", fake_tasks)
    monkeypatch.setattr(work_cmd, "task_add", fake_task_add)
    monkeypatch.setattr(work_cmd, "task_show", fake_task_show)
    monkeypatch.setattr(work_cmd, "task_plan", fake_task_plan)
    monkeypatch.setattr(work_cmd, "task_done", fake_task_done)

    assert cli.main(["work", "tasks", "--target", str(tmp_path), "--all", "--json"]) == 0
    assert (
        cli.main(
            [
                "work",
                "task",
                "add",
                "build",
                "queue",
                "--target",
                str(tmp_path),
                "--type",
                "feature",
                "--priority",
                "high",
                "--acceptance",
                "passes",
                "--template",
                "vertical-slice",
            ]
        )
        == 0
    )
    assert cli.main(["work", "task", "add", "--target", str(tmp_path), "--from-next"]) == 0
    assert cli.main(["work", "task", "show", "abc123", "--target", str(tmp_path)]) == 0
    assert cli.main(["work", "task", "plan", "abc123", "--target", str(tmp_path), "--json"]) == 0
    assert cli.main(["work", "task", "done", "abc123", "--target", str(tmp_path)]) == 0
    assert seen == [
        ("tasks", {"target": tmp_path, "all_tasks": True, "json_output": True}),
        (
            "add",
            {
                "target": tmp_path,
                "text": "build queue",
                "from_next": False,
                "from_issue": None,
                "task_type": "feature",
                "priority": "high",
                "acceptance": ["passes"],
                "template": "vertical-slice",
            },
        ),
        (
            "add",
            {
                "target": tmp_path,
                "text": None,
                "from_next": True,
                "from_issue": None,
                "task_type": "task",
                "priority": "normal",
                "acceptance": [],
                "template": None,
            },
        ),
        ("show", {"target": tmp_path, "task_id": "abc123"}),
        ("plan", {"target": tmp_path, "task_id": "abc123", "json_output": True}),
        ("done", {"target": tmp_path, "task_id": "abc123"}),
    ]


def test_work_import_cli(tmp_path, monkeypatch):
    seen = []

    def fake_import_add(**kwargs):
        seen.append(("add", kwargs))
        return 0

    def fake_import_list(**kwargs):
        seen.append(("list", kwargs))
        return 0

    def fake_import_validate(**kwargs):
        seen.append(("validate", kwargs))
        return 0

    def fake_import_ingest(**kwargs):
        seen.append(("ingest", kwargs))
        return 0

    def fake_import_memory_care(**kwargs):
        seen.append(("memory-care", kwargs))
        return 0

    def fake_import_memory_refresh(**kwargs):
        seen.append(("memory-refresh", kwargs))
        return 0

    def fake_import_chat_sweep(**kwargs):
        seen.append(("chat-sweep", kwargs))
        return 0

    def fake_import_triage(**kwargs):
        seen.append(("triage", kwargs))
        return 0

    def fake_import_show(**kwargs):
        seen.append(("show", kwargs))
        return 0

    def fake_import_plan(**kwargs):
        seen.append(("plan", kwargs))
        return 0

    def fake_import_promote(**kwargs):
        seen.append(("promote", kwargs))
        return 0

    def fake_import_dismiss(**kwargs):
        seen.append(("dismiss", kwargs))
        return 0

    monkeypatch.setattr(work_cmd, "import_add", fake_import_add)
    monkeypatch.setattr(work_cmd, "import_list", fake_import_list)
    monkeypatch.setattr(work_cmd, "import_validate", fake_import_validate)
    monkeypatch.setattr(work_cmd, "import_ingest", fake_import_ingest)
    monkeypatch.setattr(work_cmd, "import_memory_care", fake_import_memory_care)
    monkeypatch.setattr(work_cmd, "import_memory_refresh", fake_import_memory_refresh)
    monkeypatch.setattr(work_cmd, "import_chat_sweep", fake_import_chat_sweep)
    monkeypatch.setattr(work_cmd, "import_triage", fake_import_triage)
    monkeypatch.setattr(work_cmd, "import_show", fake_import_show)
    monkeypatch.setattr(work_cmd, "import_plan", fake_import_plan)
    monkeypatch.setattr(work_cmd, "import_promote", fake_import_promote)
    monkeypatch.setattr(work_cmd, "import_dismiss", fake_import_dismiss)

    assert (
        cli.main(
            [
                "work",
                "import",
                "add",
                "refresh",
                "card",
                "--target",
                str(tmp_path),
                "--kind",
                "finding",
                "--source",
                "discord",
                "--metadata",
                "channel=dev",
            ]
        )
        == 0
    )
    assert (
        cli.main(
            [
                "work",
                "import",
                "list",
                "--target",
                str(tmp_path),
                "--all",
                "--json",
                "--limit",
                "3",
                "--source",
                "handoff-ingest",
                "--kind",
                "task",
                "--metadata",
                "handoff_issue_category=skip",
            ]
        )
        == 0
    )
    assert cli.main(["work", "import", "validate", str(tmp_path / "imports.jsonl"), "--json"]) == 0
    assert (
        cli.main(
            [
                "work",
                "import",
                "ingest",
                str(tmp_path / "imports.jsonl"),
                "--target",
                str(tmp_path),
                "--dry-run",
                "--json",
            ]
        )
        == 0
    )
    assert (
        cli.main(
            [
                "work",
                "import",
                "memory-refresh",
                "--target",
                str(tmp_path),
                "--queue",
                str(tmp_path / "memory-refresh.json"),
                "--dry-run",
                "--json",
            ]
        )
        == 0
    )
    assert (
        cli.main(
            [
                "work",
                "import",
                "memory-care",
                "--target",
                str(tmp_path),
                "--queue",
                str(tmp_path / "refresh-queue.json"),
                "--dry-run",
                "--json",
            ]
        )
        == 0
    )
    assert (
        cli.main(
            [
                "work",
                "import",
                "chat-sweep",
                "--target",
                str(tmp_path),
                "--input",
                str(tmp_path / "latest-sweep.json"),
                "--dry-run",
                "--json",
            ]
        )
        == 0
    )
    assert (
        cli.main(
            [
                "work",
                "import",
                "triage",
                "--target",
                str(tmp_path),
                "--json",
                "--limit",
                "4",
                "--source",
                "handoff-ingest",
                "--metadata",
                "handoff_issue_category=route-skip",
            ]
        )
        == 0
    )
    assert cli.main(["work", "import", "show", "imp123", "--target", str(tmp_path)]) == 0
    assert cli.main(["work", "import", "plan", "imp123", "--target", str(tmp_path), "--json"]) == 0
    assert (
        cli.main(
            [
                "work",
                "import",
                "promote",
                "imp123",
                "--target",
                str(tmp_path),
                "--run",
            ]
        )
        == 0
    )
    assert (
        cli.main(
            [
                "work",
                "import",
                "dismiss",
                "--target",
                str(tmp_path),
                "--all",
                "--kind",
                "task",
                "--source",
                "handoff-ingest",
                "--metadata",
                "handoff_issue_category=skip",
                "--reason",
                "noise",
            ]
        )
        == 0
    )
    assert seen == [
        (
            "add",
            {
                "target": tmp_path,
                "text": "refresh card",
                "kind": "finding",
                "source": "discord",
                "metadata": ["channel=dev"],
            },
        ),
        (
            "list",
            {
                "target": tmp_path,
                "all_imports": True,
                "json_output": True,
                "limit": 3,
                "source": "handoff-ingest",
                "kind": "task",
                "metadata": ["handoff_issue_category=skip"],
            },
        ),
        ("validate", {"input_path": tmp_path / "imports.jsonl", "json_output": True}),
        (
            "ingest",
            {
                "target": tmp_path,
                "input_path": tmp_path / "imports.jsonl",
                "dry_run": True,
                "json_output": True,
            },
        ),
        (
            "memory-refresh",
            {
                "target": tmp_path,
                "queue": tmp_path / "memory-refresh.json",
                "dry_run": True,
                "json_output": True,
            },
        ),
        (
            "memory-care",
            {
                "target": tmp_path,
                "queue": tmp_path / "refresh-queue.json",
                "dry_run": True,
                "json_output": True,
            },
        ),
        (
            "chat-sweep",
            {
                "target": tmp_path,
                "input_path": tmp_path / "latest-sweep.json",
                "dry_run": True,
                "json_output": True,
            },
        ),
        (
            "triage",
            {
                "target": tmp_path,
                "json_output": True,
                "limit": 4,
                "source": "handoff-ingest",
                "kind": None,
                "metadata": ["handoff_issue_category=route-skip"],
            },
        ),
        ("show", {"target": tmp_path, "import_id": "imp123"}),
        ("plan", {"target": tmp_path, "import_id": "imp123", "json_output": True}),
        (
            "promote",
            {
                "target": tmp_path,
                "import_id": "imp123",
                "all_matching": False,
                "kind": None,
                "source": None,
                "metadata": [],
                "run_after": True,
            },
        ),
        (
            "dismiss",
            {
                "target": tmp_path,
                "import_id": None,
                "reason": "noise",
                "all_matching": True,
                "kind": "task",
                "source": "handoff-ingest",
                "metadata": ["handoff_issue_category=skip"],
            },
        ),
    ]


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
                "--queue-next",
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
        "queue_next": True,
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
