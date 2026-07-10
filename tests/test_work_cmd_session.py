import json
import subprocess
from datetime import datetime, timezone

from brigade import aboyeur
from brigade import cli
from brigade import center_cmd
from brigade import dogfood_cmd
from brigade import localio
from brigade import repos_cmd
from brigade import security_cmd
from brigade import work_cmd
from brigade.install import install_selection
from brigade.selection import Selection

from tests.work_cmd_test_helpers import (
    _write_json,
    _init_git_repo,
    _plan_task_id,
)


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
    monkeypatch.setattr(work_cmd.helpers.shutil, "which", lambda name: f"/usr/bin/{name}")

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
    monkeypatch.setattr(work_cmd.helpers.shutil, "which", lambda name: None)

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
    monkeypatch.setattr(work_cmd.helpers.shutil, "which", lambda name: f"/usr/bin/{name}" if name == "codex" else None)
    monkeypatch.setattr(localio, "check_git_ignored", lambda repo, path: "yes")

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
    monkeypatch.setattr(work_cmd.helpers.shutil, "which", lambda name: "/usr/bin/codex" if name == "codex" else None)
    monkeypatch.setattr(localio, "check_git_ignored", lambda repo, path: "yes")
    monkeypatch.setattr(
        work_cmd.helpers,
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
        work_cmd.helpers,
        "_now",
        lambda: datetime(2026, 5, 27, 10, 0, 0, tzinfo=timezone.utc),
    )

    assert work_cmd.doctor(target=tmp_path) == 0
    out = capsys.readouterr().out
    assert "[warn] active_session_age:" in out
    assert "[warn] task_acceptance: 1 pending task(s) missing acceptance criteria" in out
    assert "[warn] github_issues: 1 issue-backed task(s) cannot be checked because gh is missing: issue-task" in out


def test_work_doctor_reports_workflow_rule_template_visibility(tmp_path, monkeypatch, capsys):
    _init_git_repo(tmp_path)
    dogfood_cmd.init(target=tmp_path)
    monkeypatch.setattr(work_cmd.helpers.shutil, "which", lambda name: "/usr/bin/codex" if name == "codex" else None)
    monkeypatch.setattr(localio, "check_git_ignored", lambda repo, path: "yes")

    assert work_cmd.doctor(target=tmp_path) == 0
    out = capsys.readouterr().out
    assert "[warn] workflow_rules: missing rules/issue-tdd-loop.md, rules/acceptance-driven-work.md;" in out

    rc = install_selection(
        tmp_path,
        Selection(depth="repo", harnesses=[], owner="this-repo", includes=["repo-extras"]),
        force=True,
    )
    assert rc == 0
    capsys.readouterr()
    assert work_cmd.doctor(target=tmp_path) == 0
    out = capsys.readouterr().out
    assert "[ok] workflow_rules: repo-shareable workflow rules installed" in out


def test_work_doctor_warns_when_issue_backed_task_is_closed(tmp_path, monkeypatch, capsys):
    _init_git_repo(tmp_path)
    dogfood_cmd.init(target=tmp_path)
    monkeypatch.setattr(
        work_cmd.helpers.shutil,
        "which",
        lambda name: f"/usr/bin/{name}" if name in {"codex", "gh"} else None,
    )
    monkeypatch.setattr(localio, "check_git_ignored", lambda repo, path: "yes")
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

    monkeypatch.setattr(work_cmd.helpers.subprocess, "run", fake_run)

    assert work_cmd.doctor(target=tmp_path) == 0
    out = capsys.readouterr().out
    assert "[warn] github_issues_closed: 1 remote issue(s) are closed: issue-task" in out


def test_work_doctor_fails_invalid_security_config(tmp_path, monkeypatch, capsys):
    _init_git_repo(tmp_path)
    dogfood_cmd.init(target=tmp_path)
    security_config = tmp_path / ".brigade" / "security.toml"
    security_config.write_text('policy = "not-real"\n')
    monkeypatch.setattr(work_cmd.helpers.shutil, "which", lambda name: f"/usr/bin/{name}" if name == "codex" else None)
    monkeypatch.setattr(localio, "check_git_ignored", lambda repo, path: "yes")

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
    monkeypatch.setattr(work_cmd.helpers.shutil, "which", lambda name: f"/usr/bin/{name}" if name == "codex" else None)
    monkeypatch.setattr(localio, "check_git_ignored", lambda repo, path: "yes")

    assert work_cmd.doctor(target=tmp_path) == 0
    out = capsys.readouterr().out
    assert "[warn] security_stale_suppressions:" in out
    assert "[warn] security_suppression_reasons:" in out


def test_work_doctor_reports_blockers(tmp_path, monkeypatch, capsys):
    _init_git_repo(tmp_path)
    monkeypatch.setattr(work_cmd.helpers.shutil, "which", lambda name: None)

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
        work_cmd.helpers,
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
        work_cmd.helpers,
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
    monkeypatch.setattr(work_cmd.helpers, "_now", lambda: next(times))
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
    monkeypatch.setattr(work_cmd.helpers, "_now", lambda: next(times))
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
    monkeypatch.setattr(work_cmd.helpers, "_now", lambda: next(times))
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
    monkeypatch.setattr(work_cmd.helpers, "_now", lambda: next(times))
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
    monkeypatch.setattr(work_cmd.helpers, "_now", lambda: next(times))
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
    monkeypatch.setattr(work_cmd.helpers, "_now", lambda: next(times))
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
        work_cmd.helpers,
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
    monkeypatch.setattr(work_cmd.helpers, "_now", lambda: next(times))
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
        work_cmd.helpers,
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
    monkeypatch.setattr(work_cmd.helpers, "_now", lambda: next(times))
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
    monkeypatch.setattr(work_cmd.helpers, "_now", lambda: next(times))
    monkeypatch.setattr(work_cmd.helpers.shutil, "which", lambda name: f"/usr/bin/{name}")
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
        work_cmd.helpers,
        "_now",
        lambda: datetime(2026, 5, 26, 12, 0, 0, tzinfo=timezone.utc),
    )
    monkeypatch.setattr(work_cmd.helpers.shutil, "which", lambda name: f"/usr/bin/{name}")
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


def test_work_brief_json_attaches_graphtrail_context_for_selected_task(tmp_path, monkeypatch, capsys):
    _init_git_repo(tmp_path)
    db = tmp_path / ".graphtrail" / "graphtrail.db"
    db.parent.mkdir()
    db.write_text("")
    graphtrail = tmp_path / "fake-graphtrail"
    graphtrail.write_text("#!/bin/sh\nprintf '%s\\n' '### Entry points' '- brigade.work_cmd.session._brief_payload'\n")
    graphtrail.chmod(0o755)
    monkeypatch.setenv("GRAPHTRAIL_BIN", str(graphtrail))
    assert work_cmd.task_add(target=tmp_path, text="Attach GraphTrail to work brief") == 0
    capsys.readouterr()

    assert cli.main(["work", "brief", "--target", str(tmp_path), "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    context = payload["code_graph_context"]

    assert "brigade.work_cmd.session._brief_payload" in context
    assert payload["code_graph_brief"] == {
        "attached": True,
        "bytes": len(context.encode()),
    }


def test_plain_work_brief_skips_graphtrail_while_json_attaches(tmp_path, monkeypatch, capsys):
    _init_git_repo(tmp_path)
    assert work_cmd.task_add(target=tmp_path, text="Attach GraphTrail only to JSON brief") == 0
    capsys.readouterr()
    calls = []
    text = "## Code graph context (GraphTrail, read-only)\n\nselected task context\n"

    def fake_code_graph_brief(target, task):
        calls.append((target, task))
        return aboyeur.CodeGraphBrief(attached=True, text=text, bytes=len(text.encode()))

    monkeypatch.setattr(aboyeur, "code_graph_brief", fake_code_graph_brief)

    assert cli.main(["work", "brief", "--target", str(tmp_path)]) == 0
    capsys.readouterr()
    assert calls == []

    assert cli.main(["work", "brief", "--target", str(tmp_path), "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert calls == [(tmp_path.resolve(), "Attach GraphTrail only to JSON brief")]
    assert payload["code_graph_context"] == text
    assert payload["code_graph_brief"] == {"attached": True, "bytes": len(text.encode())}


def test_work_brief_json_skips_graphtrail_without_selected_task(tmp_path, monkeypatch, capsys):
    _init_git_repo(tmp_path)

    def fail_if_called(target, task):
        raise AssertionError(f"unexpected GraphTrail call for {target}: {task}")

    monkeypatch.setattr(aboyeur, "code_graph_brief", fail_if_called)

    assert cli.main(["work", "brief", "--target", str(tmp_path), "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)

    assert payload["code_graph_context"] is None
    assert payload["code_graph_brief"] == {"attached": False, "bytes": 0}


def test_work_brief_json_compacts_heavy_report_and_fleet_health(tmp_path, monkeypatch, capsys):
    _init_git_repo(tmp_path)
    dogfood_cmd.init(target=tmp_path)
    capsys.readouterr()
    monkeypatch.setattr(work_cmd.helpers.shutil, "which", lambda name: f"/usr/bin/{name}")
    monkeypatch.setattr(
        center_cmd,
        "report_health",
        lambda target: {
            "issue_count": 0,
            "top_issue": None,
            "latest": {
                "report_id": "operator-report-1",
                "created_at": "2026-06-08T12:00:00+00:00",
                "activity": [{"id": f"activity-{index}", "safe_summary": "large"} for index in range(100)],
                "reviews": [{"id": "review-1"}],
            },
            "latest_diff": {
                "report_id": "report-diff-1",
                "status": "unchanged",
                "activity": [{"id": "diff-activity"}],
            },
        },
    )
    monkeypatch.setattr(
        repos_cmd,
        "health",
        lambda target: {
            "config_path": str(tmp_path / ".brigade" / "repos.toml"),
            "repo_count": 20,
            "issue_count": 0,
            "top_issue": None,
            "report": {"issue_count": 0, "latest": {"report_id": "fleet-report-1", "repos": [{"id": "repo"}]}},
            "actions": {"open_count": 1, "top_action": {"id": "action-1", "safe_summary": "dispatch"}},
            "sweep": {
                "issue_count": 0,
                "latest": {"sweep_id": "sweep-1", "repos": [{"id": f"repo-{index}"} for index in range(50)]},
            },
            "release_train": {
                "issue_count": 0,
                "latest": {
                    "train_id": "train-1",
                    "status": "reviewed",
                    "repos": [{"repo_id": f"repo-{index}", "details": "large"} for index in range(50)],
                    "suggested_next_commands": ["brigade release doctor"],
                },
                "actions": {"open_count": 2, "top_action": {"id": "release-action-1", "safe_summary": "release"}},
                "evidence": {"record_count": 3, "top_issue": None},
            },
        },
    )

    assert work_cmd.brief(target=tmp_path, json_output=True) == 0
    payload = json.loads(capsys.readouterr().out)

    latest_report = payload["operator_report"]["latest"]
    assert latest_report["report_id"] == "operator-report-1"
    assert latest_report["activity_count"] == 100
    assert latest_report["review_count"] == 1
    assert "activity" not in latest_report
    assert "reviews" not in latest_report

    latest_train = payload["repo_fleet"]["release_train"]["latest"]
    assert latest_train["train_id"] == "train-1"
    assert latest_train["repo_count"] == 50
    assert "repos" not in latest_train
    latest_sweep = payload["repo_fleet"]["sweep"]["latest"]
    assert latest_sweep["sweep_id"] == "sweep-1"
    assert latest_sweep["repo_count"] == 50
    assert "repos" not in latest_sweep


def test_work_doctor_warns_for_plan_coverage(tmp_path, monkeypatch, capsys):
    _init_git_repo(tmp_path)
    dogfood_cmd.init(target=tmp_path)
    monkeypatch.setattr(work_cmd.helpers.shutil, "which", lambda name: "/usr/bin/codex" if name == "codex" else None)
    monkeypatch.setattr(localio, "check_git_ignored", lambda repo, path: "yes")
    task_id = _plan_task_id(tmp_path, capsys)
    capsys.readouterr()

    assert work_cmd.doctor(target=tmp_path) == 0
    out = capsys.readouterr().out
    assert f"[warn] plan_coverage: 1 significant pending task(s) without a plan artifact: {task_id}" in out

    assert work_cmd.task_plan(target=tmp_path, task_id=task_id[:12], write=True) == 0
    capsys.readouterr()
    assert work_cmd.doctor(target=tmp_path) == 0
    out = capsys.readouterr().out
    assert "[ok] plan_coverage: significant pending tasks have plan artifacts" in out


def test_brief_payload_includes_plan_coverage(tmp_path, capsys):
    _init_git_repo(tmp_path)
    task_id = _plan_task_id(tmp_path, capsys)
    capsys.readouterr()

    payload = work_cmd._brief_payload(tmp_path)
    assert payload["plan_coverage"] == {
        "pending_total": 1,
        "significant_without_plan": 1,
        "task_ids": [task_id],
    }

    assert work_cmd.brief(target=tmp_path, json_output=True) == 0
    json_payload = json.loads(capsys.readouterr().out)
    assert json_payload["plan_coverage"]["significant_without_plan"] == 1
    assert json_payload["plan_coverage"]["task_ids"] == [task_id]

    assert work_cmd.brief(target=tmp_path) == 0
    out = capsys.readouterr().out
    assert f"plans: 1 pending task(s) without a plan artifact ({task_id})" in out


def test_work_plans_lists_newest_first_and_empty(tmp_path, capsys):
    _init_git_repo(tmp_path)
    # empty case
    assert work_cmd.plans(target=tmp_path) == 0
    assert "no plan artifacts" in capsys.readouterr().out
    assert work_cmd.plans(target=tmp_path, json_output=True) == 0
    assert json.loads(capsys.readouterr().out) == []

    first = _plan_task_id(tmp_path, capsys, text="First task")
    assert work_cmd.task_plan(target=tmp_path, task_id=first, write=True) == 0
    capsys.readouterr()
    import time as _time

    _time.sleep(0.01)
    second = _plan_task_id(tmp_path, capsys, text="Second task")
    assert work_cmd.task_plan(target=tmp_path, task_id=second, write=True, accept=True) == 0
    capsys.readouterr()

    assert work_cmd.plans(target=tmp_path, json_output=True) == 0
    entries = json.loads(capsys.readouterr().out)
    assert len(entries) == 2
    assert entries[0]["task_id"] == second
    assert entries[1]["task_id"] == first
    assert entries[0]["status"] == "accepted"
    assert entries[0]["kind"] == "plan"
    assert entries[1]["kind"] == "plan"
    assert entries[0]["path"] == f".brigade/work/plans/{second}.plan.md"

    assert work_cmd.plans(target=tmp_path) == 0
    text_out = capsys.readouterr().out
    assert second in text_out
    assert first in text_out


def test_work_plans_unreadable_json_does_not_crash(tmp_path, capsys):
    _init_git_repo(tmp_path)
    task_id = _plan_task_id(tmp_path, capsys)
    assert work_cmd.task_plan(target=tmp_path, task_id=task_id[:12], write=True) == 0
    capsys.readouterr()
    json_path, _ = work_cmd._plan_paths(tmp_path, task_id)
    json_path.write_text("{not valid json")

    assert work_cmd.plans(target=tmp_path, json_output=True) == 0
    entries = json.loads(capsys.readouterr().out)
    assert len(entries) == 1
    assert entries[0]["status"] == "unreadable"


def test_work_plans_lists_both_kinds(tmp_path, capsys):
    _init_git_repo(tmp_path)
    task_id = _plan_task_id(tmp_path, capsys, text="Coexisting kinds")
    assert work_cmd.task_plan(target=tmp_path, task_id=task_id[:12], write=True) == 0
    capsys.readouterr()
    assert work_cmd.task_plan(target=tmp_path, task_id=task_id[:12], write=True, kind="meta") == 0
    capsys.readouterr()

    assert work_cmd.plans(target=tmp_path, json_output=True) == 0
    entries = json.loads(capsys.readouterr().out)
    assert len(entries) == 2
    kinds = {entry["kind"] for entry in entries}
    assert kinds == {"plan", "meta"}
    for entry in entries:
        assert entry["task_id"] == task_id

    assert work_cmd.plans(target=tmp_path) == 0
    text_out = capsys.readouterr().out
    assert "[plan]" in text_out
    assert "[meta]" in text_out


def test_work_doctor_warns_on_unclosed_review_run(tmp_path, monkeypatch, capsys):
    _init_git_repo(tmp_path)
    dogfood_cmd.init(target=tmp_path)
    monkeypatch.setattr(work_cmd.helpers.shutil, "which", lambda name: f"/usr/bin/{name}" if name == "codex" else None)
    monkeypatch.setattr(localio, "check_git_ignored", lambda repo, path: "yes")
    run_dir = tmp_path / ".brigade" / "reviews" / "runs" / "run-unclosed"
    run_dir.mkdir(parents=True)
    _write_json(
        run_dir / "receipt.json",
        {
            "run_id": "run-unclosed",
            "reviewer_id": "codex-review",
            "status": "completed",
            "exit_code": 0,
            "started_at": "2026-05-28T12:00:00+00:00",
            "completed_at": "2026-05-28T12:01:00+00:00",
            "path": str(run_dir),
        },
    )

    assert work_cmd.doctor(target=tmp_path) == 0
    out = capsys.readouterr().out
    assert "[warn] review_runs_unclosed: run-unclosed" in out


def test_work_next_reports_latest_next_as_default_task(tmp_path, monkeypatch, capsys):
    _init_git_repo(tmp_path)
    dogfood_cmd.init(target=tmp_path)
    run_dir = tmp_path / ".brigade" / "runs" / "latest"
    run_dir.mkdir(parents=True)
    _write_json(run_dir / "run.json", {"started_at": "2026-05-26T12:10:00Z", "status": "ok", "task": "review"})
    (run_dir / "final.txt").write_text("Done.\n\nNext step: Build next command.\n")
    monkeypatch.setattr(work_cmd.helpers.shutil, "which", lambda name: f"/usr/bin/{name}")

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
    monkeypatch.setattr(work_cmd.helpers.shutil, "which", lambda name: f"/usr/bin/{name}")
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
    monkeypatch.setattr(work_cmd.helpers.shutil, "which", lambda name: f"/usr/bin/{name}" if name == "codex" else None)

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
    monkeypatch.setattr(work_cmd.helpers.shutil, "which", lambda name: f"/usr/bin/{name}" if name == "codex" else None)

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
    monkeypatch.setattr(work_cmd.helpers.shutil, "which", lambda name: None)

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
    monkeypatch.setattr(work_cmd.helpers, "_now", lambda: next(times))
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
    monkeypatch.setattr(work_cmd.helpers, "_now", lambda: next(times))
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
    monkeypatch.setattr(work_cmd.helpers, "_now", lambda: next(times))
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
    monkeypatch.setattr(work_cmd.helpers, "_now", lambda: next(times))
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
    monkeypatch.setattr(work_cmd.helpers, "_now", lambda: next(times))
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
    monkeypatch.setattr(work_cmd.helpers, "_now", lambda: next(times))

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
    monkeypatch.setattr(work_cmd.helpers, "_now", lambda: next(times))
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
    monkeypatch.setattr(work_cmd.helpers, "_now", lambda: next(times))
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
    monkeypatch.setattr(work_cmd.helpers, "_now", lambda: next(times))
    assert (
        work_cmd.task_add(target=tmp_path, text="Build pending failure", acceptance=["Do not complete on failure"]) == 0
    )
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
