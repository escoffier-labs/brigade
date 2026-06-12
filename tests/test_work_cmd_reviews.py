import json
import sys
from pathlib import Path

from brigade import cli
from brigade import work_cmd

from tests.work_cmd_test_helpers import (
    _write_json,
    _init_git_repo,
)


def test_work_review_init_and_plan_text_and_json(tmp_path, capsys):
    _init_git_repo(tmp_path)

    assert work_cmd.review_init(target=tmp_path, update_gitignore=False) == 0
    out = capsys.readouterr().out
    assert "review_config:" in out
    assert "reviewers: 3" in out
    assert (tmp_path / ".brigade" / "reviews.toml").is_file()

    assert work_cmd.review_plan(target=tmp_path) == 0
    out = capsys.readouterr().out
    assert "work review plan:" in out
    assert "codex-review" in out
    assert "claude-opus-review" in out

    assert work_cmd.review_plan(target=tmp_path, json_output=True) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["valid"] is True
    assert {item["id"] for item in payload["planned"]} >= {"codex-review", "claude-opus-review", "custom"}


def test_work_review_run_writes_receipts_for_fake_codex_and_claude(tmp_path, capsys):
    _init_git_repo(tmp_path)
    script = tmp_path / "reviewer.py"
    script.write_text(
        """
import json
import sys
from pathlib import Path

findings = Path(sys.argv[1])
reviewer = sys.argv[2]
findings.parent.mkdir(parents=True, exist_ok=True)
findings.write_text(json.dumps({"findings": [{"id": reviewer + "-1", "severity": "high", "category": "bug", "path": "src/app.py", "line": 12, "safe_excerpt": "return value", "rationale": "The return value is wrong.", "suggested_fix": "Return the computed value.", "confidence": "high"}]}) + "\\n")
print("review complete " + reviewer)
"""
    )
    config = tmp_path / ".brigade" / "reviews.toml"
    config.parent.mkdir(parents=True)
    config.write_text(
        f"""
[[reviewer]]
id = "codex-review"
name = "Codex review"
command = "{sys.executable} {script} .brigade/reviews/codex-findings.json codex"
cwd = "."
enabled = true
timeout = 30
target_paths = ["."]
base_ref = "HEAD"
output_path = ".brigade/reviews/codex-output.json"
findings_path = ".brigade/reviews/codex-findings.json"
supported_modes = ["diff"]
privacy_mode = "safe-summary"

[[reviewer]]
id = "claude-opus-review"
name = "Claude Opus review"
command = "{sys.executable} {script} .brigade/reviews/claude-findings.json claude"
cwd = "."
enabled = true
timeout = 30
target_paths = ["."]
base_ref = "HEAD"
output_path = ".brigade/reviews/claude-output.json"
findings_path = ".brigade/reviews/claude-findings.json"
supported_modes = ["diff", "subagents"]
privacy_mode = "safe-summary"
"""
    )

    assert work_cmd.review_run(target=tmp_path, all_matching=True, json_output=True) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["completed"] == 2
    assert {run["reviewer_id"] for run in payload["runs"]} == {"codex-review", "claude-opus-review"}
    assert all(Path(run["stdout_path"]).is_file() for run in payload["runs"])
    assert all(Path(run["stderr_path"]).is_file() for run in payload["runs"])
    assert all(run["stdout_summary"].startswith("review complete") for run in payload["runs"])

    assert work_cmd.review_runs(target=tmp_path, json_output=True) == 0
    runs_payload = json.loads(capsys.readouterr().out)
    assert len(runs_payload["runs"]) == 2
    run_id = runs_payload["runs"][0]["run_id"]

    assert work_cmd.review_show(target=tmp_path, run_id=run_id) == 0
    out = capsys.readouterr().out
    assert f"review_run: {run_id}" in out
    assert "status: completed" in out


def test_work_review_run_covers_timeout_nonzero_and_malformed_findings(tmp_path, monkeypatch, capsys):
    _init_git_repo(tmp_path)
    monkeypatch.setattr(work_cmd.helpers.shutil, "which", lambda name: f"/usr/bin/{name}")
    script = tmp_path / "reviewer.py"
    script.write_text(
        """
import sys
import time
from pathlib import Path

mode = sys.argv[1]
if mode == "timeout":
    time.sleep(1)
elif mode == "fail":
    print("bad review")
    print("review error", file=sys.stderr)
    raise SystemExit(8)
elif mode == "malformed":
    path = Path(".brigade/reviews/malformed.json")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{bad json\\n")
"""
    )
    config = tmp_path / ".brigade" / "reviews.toml"
    config.parent.mkdir(parents=True)
    config.write_text(
        f"""
[[reviewer]]
id = "timeout-review"
name = "Timeout review"
command = "{sys.executable} {script} timeout"
cwd = "."
enabled = true
timeout = 0.01
target_paths = ["."]
base_ref = "HEAD"
output_path = ".brigade/reviews/timeout-output.json"
findings_path = ".brigade/reviews/timeout.json"
supported_modes = ["diff"]
privacy_mode = "safe-summary"

[[reviewer]]
id = "fail-review"
name = "Fail review"
command = "{sys.executable} {script} fail"
cwd = "."
enabled = true
timeout = 30
target_paths = ["."]
base_ref = "HEAD"
output_path = ".brigade/reviews/fail-output.json"
findings_path = ".brigade/reviews/fail.json"
supported_modes = ["diff"]
privacy_mode = "safe-summary"

[[reviewer]]
id = "malformed-review"
name = "Malformed review"
command = "{sys.executable} {script} malformed"
cwd = "."
enabled = true
timeout = 30
target_paths = ["."]
base_ref = "HEAD"
output_path = ".brigade/reviews/malformed-output.json"
findings_path = ".brigade/reviews/malformed.json"
supported_modes = ["diff"]
privacy_mode = "safe-summary"
"""
    )

    assert work_cmd.review_run(target=tmp_path, reviewer_id="timeout-review", json_output=True) == 1
    timeout_payload = json.loads(capsys.readouterr().out)
    assert timeout_payload["runs"][0]["timed_out"] is True

    assert work_cmd.review_run(target=tmp_path, reviewer_id="fail-review", json_output=True) == 1
    fail_payload = json.loads(capsys.readouterr().out)
    assert fail_payload["runs"][0]["exit_code"] == 8
    assert fail_payload["runs"][0]["stdout_summary"] == "bad review"
    assert fail_payload["runs"][0]["stderr_summary"] == "review error"

    assert work_cmd.review_run(target=tmp_path, reviewer_id="malformed-review", json_output=True) == 0
    malformed_payload = json.loads(capsys.readouterr().out)
    assert (
        work_cmd.review_import_findings(
            target=tmp_path, run_id=malformed_payload["runs"][0]["run_id"], json_output=True
        )
        == 2
    )
    assert "invalid JSON" in json.loads(capsys.readouterr().out)["errors"][0]

    assert work_cmd.doctor(target=tmp_path) == 1
    out = capsys.readouterr().out
    assert "review_runs_failed" in out
    assert "review_findings_malformed" in out


def test_work_review_import_findings_dedupes_dismissed_and_redacts(tmp_path, capsys):
    _init_git_repo(tmp_path)
    run_dir = tmp_path / ".brigade" / "reviews" / "runs" / "run-one"
    run_dir.mkdir(parents=True)
    findings_path = tmp_path / ".brigade" / "reviews" / "findings.json"
    findings_path.parent.mkdir(parents=True, exist_ok=True)
    findings_path.write_text(
        json.dumps(
            {
                "findings": [
                    {
                        "id": "finding-one",
                        "severity": "medium",
                        "category": "maintainability",
                        "path": "src/app.py",
                        "line": 4,
                        "safe_excerpt": "token=supersecretvalue",
                        "rationale": "Refactor this path and do not expose token=supersecretvalue.",
                        "suggested_fix": "Extract a helper.",
                        "confidence": "medium",
                        "raw_output": "private transcript should not appear",
                    }
                ]
            }
        )
        + "\n"
    )
    _write_json(
        run_dir / "receipt.json",
        {
            "run_id": "run-one",
            "reviewer_id": "codex-review",
            "status": "completed",
            "exit_code": 0,
            "started_at": "2026-05-28T12:00:00+00:00",
            "completed_at": "2026-05-28T12:01:00+00:00",
            "path": str(run_dir),
            "findings_path": str(findings_path),
        },
    )

    assert work_cmd.review_import_findings(target=tmp_path, run_id="run-one", json_output=True) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["created"] == 1
    assert payload["skipped"] == 0
    stored = (tmp_path / ".brigade" / "work" / "imports" / "inbox.jsonl").read_text()
    assert "private transcript should not appear" not in stored
    assert "supersecretvalue" not in stored
    assert "[redacted]" in stored

    assert work_cmd.review_import_findings(target=tmp_path, run_id="run-one", json_output=True) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["created"] == 0
    assert payload["skipped"] == 1

    imports = work_cmd._read_imports(tmp_path)
    imports[0]["status"] = "dismissed"
    work_cmd._write_imports(tmp_path, imports)
    assert work_cmd.review_import_findings(target=tmp_path, run_id="run-one", json_output=True) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["created"] == 0
    assert payload["dismissed"] == 1

    findings = json.loads(findings_path.read_text())
    findings["findings"][0]["rationale"] = "The source item changed materially."
    findings_path.write_text(json.dumps(findings) + "\n")
    assert work_cmd.review_import_findings(target=tmp_path, run_id="run-one", json_output=True) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["created"] == 1


def test_work_review_findings_list_show_text_and_json(tmp_path, capsys):
    _init_git_repo(tmp_path)
    item = work_cmd._make_import(
        "Review finding high bug in src/app.py: Fix regression.",
        kind="task",
        source="code-review",
        priority="high",
        metadata={
            "reviewer_id": "codex-review",
            "review_run_id": "run-one",
            "review_finding_id": "finding-one",
            "severity": "high",
            "category": "bug",
            "path": "src/app.py",
            "line": 10,
            "source_item_key": "code-review:codex-review:finding-one",
            "source_fingerprint": "fp-one",
        },
    )
    work_cmd._write_imports(tmp_path, [item])

    assert work_cmd.review_findings(target=tmp_path, json_output=True) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["count"] == 1
    assert payload["unresolved_count"] == 1
    assert payload["groups"]["by_reviewer"] == {"codex-review": 1}
    assert payload["groups"]["by_run"] == {"run-one": 1}
    assert payload["groups"]["by_resolution"] == {"pending": 1}

    assert work_cmd.review_findings(target=tmp_path) == 0
    out = capsys.readouterr().out
    assert "review findings:" in out
    assert "by_reviewer:" in out
    assert "finding-one" in out

    assert work_cmd.review_finding_show(target=tmp_path, finding_id="finding-one", json_output=True) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["finding"]["import_id"] == item["id"]
    assert payload["finding"]["resolution_state"] == "pending"

    assert work_cmd.review_finding_show(target=tmp_path, finding_id=item["id"]) == 0
    out = capsys.readouterr().out
    assert "review_finding: finding-one" in out
    assert "resolution_state: pending" in out


def test_work_review_closeout_clean_run(tmp_path, capsys):
    _init_git_repo(tmp_path)
    run_dir = tmp_path / ".brigade" / "reviews" / "runs" / "run-clean"
    run_dir.mkdir(parents=True)
    findings_path = tmp_path / ".brigade" / "reviews" / "clean-findings.json"
    findings_path.parent.mkdir(parents=True, exist_ok=True)
    findings_path.write_text(json.dumps({"findings": []}) + "\n")
    _write_json(
        run_dir / "receipt.json",
        {
            "run_id": "run-clean",
            "reviewer_id": "codex-review",
            "status": "completed",
            "exit_code": 0,
            "started_at": "2026-05-28T12:00:00+00:00",
            "completed_at": "2026-05-28T12:01:00+00:00",
            "path": str(run_dir),
            "findings_path": str(findings_path),
        },
    )

    assert work_cmd.review_closeout(target=tmp_path, run_id="latest", json_output=True) == 0
    payload = json.loads(capsys.readouterr().out)
    closeout = payload["closeout"]
    assert closeout["resolved"] is True
    assert closeout["finding_count"] == 0
    assert closeout["unresolved_count"] == 0
    assert (run_dir / "closeout.json").is_file()
    receipt = json.loads((run_dir / "receipt.json").read_text())
    assert receipt["closeout"]["resolved"] is True


def test_work_review_closeout_tracks_resolution_states_and_changed_fingerprints(tmp_path, capsys):
    _init_git_repo(tmp_path)
    run_dir = tmp_path / ".brigade" / "reviews" / "runs" / "run-closeout"
    run_dir.mkdir(parents=True)
    findings_path = tmp_path / ".brigade" / "reviews" / "findings.json"
    findings_path.parent.mkdir(parents=True, exist_ok=True)
    findings_path.write_text(
        json.dumps(
            {
                "findings": [
                    {
                        "id": "pending-one",
                        "severity": "medium",
                        "category": "bug",
                        "path": "a.py",
                        "rationale": "Pending.",
                        "source_fingerprint": "fp-pending",
                    },
                    {
                        "id": "dismissed-one",
                        "severity": "low",
                        "category": "docs",
                        "path": "b.py",
                        "rationale": "Dismissed.",
                        "source_fingerprint": "fp-dismissed",
                    },
                    {
                        "id": "promoted-one",
                        "severity": "high",
                        "category": "bug",
                        "path": "c.py",
                        "rationale": "Promoted.",
                        "source_fingerprint": "fp-promoted",
                    },
                    {
                        "id": "completed-one",
                        "severity": "high",
                        "category": "bug",
                        "path": "d.py",
                        "rationale": "Completed.",
                        "source_fingerprint": "fp-completed",
                    },
                    {
                        "id": "changed-one",
                        "severity": "medium",
                        "category": "bug",
                        "path": "e.py",
                        "rationale": "Changed.",
                        "source_fingerprint": "fp-new",
                    },
                ]
            }
        )
        + "\n"
    )
    _write_json(
        run_dir / "receipt.json",
        {
            "run_id": "run-closeout",
            "reviewer_id": "codex-review",
            "status": "completed",
            "exit_code": 0,
            "started_at": "2026-05-28T12:00:00+00:00",
            "completed_at": "2026-05-28T12:01:00+00:00",
            "path": str(run_dir),
            "findings_path": str(findings_path),
        },
    )
    records = []
    for finding_id, status, fingerprint in (
        ("pending-one", "pending", "fp-pending"),
        ("dismissed-one", "dismissed", "fp-dismissed"),
        ("promoted-one", "pending", "fp-promoted"),
        ("completed-one", "pending", "fp-completed"),
        ("changed-one", "dismissed", "fp-old"),
    ):
        item = work_cmd._make_import(
            f"Review finding {finding_id}",
            kind="task",
            source="code-review",
            metadata={
                "reviewer_id": "codex-review",
                "review_run_id": "run-closeout",
                "review_finding_id": finding_id,
                "severity": "high",
                "category": "bug",
                "path": f"{finding_id}.py",
                "source_item_key": f"code-review:codex-review:{finding_id}",
                "source_fingerprint": fingerprint,
            },
        )
        item["status"] = status
        if status == "dismissed":
            item["dismiss_reason"] = "not actionable"
        records.append(item)
    work_cmd._write_imports(tmp_path, records)

    promoted_task, _ = work_cmd._mark_import_promoted(tmp_path, records[2])
    completed_task, _ = work_cmd._mark_import_promoted(tmp_path, records[3])
    completed_task["status"] = "done"
    completed_task["completed_at"] = "2026-05-28T12:05:00+00:00"
    ledger = work_cmd._read_task_ledger(tmp_path)
    for task in ledger["tasks"]:
        if task["id"] == completed_task["id"]:
            task.update(completed_task)
    work_cmd._write_task_ledger(tmp_path, ledger)
    work_cmd._write_imports(tmp_path, records)

    assert work_cmd.review_closeout(target=tmp_path, run_id="run-closeout", json_output=True) == 1
    payload = json.loads(capsys.readouterr().out)
    closeout = payload["closeout"]
    assert closeout["pending_imports"] == 1
    assert closeout["dismissed_findings"] == 2
    assert closeout["promoted_tasks"] == 2
    assert closeout["completed_tasks"] == 1
    assert closeout["changed_source_count"] == 1
    states = {item["finding_id"]: item["resolution_state"] for item in closeout["findings"]}
    assert states["pending-one"] == "pending"
    assert states["dismissed-one"] == "dismissed"
    assert states["promoted-one"] == "promoted"
    assert states["completed-one"] == "completed"
    assert states["changed-one"] == "re_review"


def test_work_review_closeout_stamps_completed_task_and_session(tmp_path, capsys):
    _init_git_repo(tmp_path)
    session_dir = tmp_path / ".brigade" / "work" / "session-one"
    session_dir.mkdir(parents=True)
    _write_json(
        session_dir / "session.json",
        {"id": "session-one", "status": "ended", "started_at": "2026-05-28T12:00:00+00:00"},
    )
    run_dir = tmp_path / ".brigade" / "reviews" / "runs" / "run-done"
    run_dir.mkdir(parents=True)
    findings_path = tmp_path / ".brigade" / "reviews" / "done-findings.json"
    findings_path.parent.mkdir(parents=True, exist_ok=True)
    findings_path.write_text(
        json.dumps(
            {
                "findings": [
                    {
                        "id": "done-one",
                        "severity": "high",
                        "category": "bug",
                        "path": "x.py",
                        "rationale": "Fixed.",
                        "source_fingerprint": "fp-done",
                    }
                ]
            }
        )
        + "\n"
    )
    _write_json(
        run_dir / "receipt.json",
        {
            "run_id": "run-done",
            "reviewer_id": "codex-review",
            "status": "completed",
            "exit_code": 0,
            "started_at": "2026-05-28T12:00:00+00:00",
            "completed_at": "2026-05-28T12:01:00+00:00",
            "path": str(run_dir),
            "findings_path": str(findings_path),
        },
    )
    item = work_cmd._make_import(
        "Review finding done-one",
        kind="task",
        source="code-review",
        metadata={
            "reviewer_id": "codex-review",
            "review_run_id": "run-done",
            "review_finding_id": "done-one",
            "severity": "high",
            "category": "bug",
            "path": "x.py",
            "source_item_key": "code-review:codex-review:done-one",
            "source_fingerprint": "fp-done",
        },
    )
    task, _ = work_cmd._mark_import_promoted(tmp_path, item)
    task["status"] = "done"
    task["completed_at"] = "2026-05-28T12:05:00+00:00"
    ledger = work_cmd._read_task_ledger(tmp_path)
    for stored in ledger["tasks"]:
        if stored["id"] == task["id"]:
            stored.update(task)
    work_cmd._write_task_ledger(tmp_path, ledger)
    work_cmd._write_imports(tmp_path, [item])

    assert work_cmd.review_closeout(target=tmp_path, run_id="run-done", json_output=True) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["closeout"]["stamped_task_ids"] == [task["id"]]
    assert payload["closeout"]["stamped_session_path"] == str(session_dir)

    assert work_cmd.task_show(target=tmp_path, task_id=task["id"]) == 0
    out = capsys.readouterr().out
    assert "review_closeouts: 1" in out
    assert "run-done resolved=True findings=1 unresolved=0" in out
    session = json.loads((session_dir / "session.json").read_text())
    assert session["review_closeouts"][0]["review_run_id"] == "run-done"


def test_work_brief_reports_code_review_status_and_top_finding(tmp_path, monkeypatch, capsys):
    _init_git_repo(tmp_path)
    monkeypatch.setattr(work_cmd.helpers.shutil, "which", lambda name: f"/usr/bin/{name}")
    config = tmp_path / ".brigade" / "reviews.toml"
    config.parent.mkdir(parents=True)
    config.write_text(
        """
[[reviewer]]
id = "codex-review"
name = "Codex review"
command = "brigade dogfood"
cwd = "."
enabled = true
timeout = 30
target_paths = ["."]
base_ref = "HEAD"
output_path = ".brigade/reviews/output.json"
findings_path = ".brigade/reviews/findings.json"
supported_modes = ["diff"]
privacy_mode = "safe-summary"
"""
    )
    run_dir = tmp_path / ".brigade" / "reviews" / "runs" / "review-run"
    run_dir.mkdir(parents=True)
    _write_json(
        run_dir / "receipt.json",
        {
            "run_id": "review-run",
            "reviewer_id": "codex-review",
            "status": "completed",
            "exit_code": 0,
            "started_at": "2026-05-28T12:00:00+00:00",
            "completed_at": "2026-05-28T12:01:00+00:00",
            "path": str(run_dir),
            "stdout_path": str(run_dir / "stdout.log"),
            "stderr_path": str(run_dir / "stderr.log"),
        },
    )
    (run_dir / "stdout.log").write_text("ok\n")
    (run_dir / "stderr.log").write_text("")
    work_cmd._append_import_records(
        tmp_path,
        [
            {
                "text": "Review finding high bug in src/app.py: Fix regression.",
                "kind": "task",
                "source": "code-review",
                "priority": "high",
                "metadata": {"source_item_key": "code-review:codex-review:finding-one", "source_fingerprint": "fp-one"},
            }
        ],
    )

    assert work_cmd.brief(target=tmp_path, json_output=True) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["code_review"]["latest_run"]["run_id"] == "review-run"
    assert payload["code_review"]["pending_finding_count"] == 1
    assert payload["code_review"]["top_pending_finding"]["source"] == "code-review"

    assert work_cmd.brief(target=tmp_path) == 0
    out = capsys.readouterr().out
    assert "review_latest: review-run codex-review [completed]" in out
    assert "review_unclosed: review-run codex-review" in out
    assert "review_pending_findings: 1" in out
    assert "review_unresolved_findings: 1" in out
    assert "review_top_command: brigade work review finding-show" in out


def test_work_review_cli(tmp_path, monkeypatch):
    seen = []

    def fake_review_init(**kwargs):
        seen.append(("init", kwargs))
        return 0

    def fake_review_plan(**kwargs):
        seen.append(("plan", kwargs))
        return 0

    def fake_review_run(**kwargs):
        seen.append(("run", kwargs))
        return 0

    def fake_review_runs(**kwargs):
        seen.append(("runs", kwargs))
        return 0

    def fake_review_show(**kwargs):
        seen.append(("show", kwargs))
        return 0

    def fake_review_import_findings(**kwargs):
        seen.append(("import-findings", kwargs))
        return 0

    def fake_review_findings(**kwargs):
        seen.append(("findings", kwargs))
        return 0

    def fake_review_finding_show(**kwargs):
        seen.append(("finding-show", kwargs))
        return 0

    def fake_review_closeout(**kwargs):
        seen.append(("closeout", kwargs))
        return 0

    monkeypatch.setattr(work_cmd, "review_init", fake_review_init)
    monkeypatch.setattr(work_cmd, "review_plan", fake_review_plan)
    monkeypatch.setattr(work_cmd, "review_run", fake_review_run)
    monkeypatch.setattr(work_cmd, "review_runs", fake_review_runs)
    monkeypatch.setattr(work_cmd, "review_show", fake_review_show)
    monkeypatch.setattr(work_cmd, "review_import_findings", fake_review_import_findings)
    monkeypatch.setattr(work_cmd, "review_findings", fake_review_findings)
    monkeypatch.setattr(work_cmd, "review_finding_show", fake_review_finding_show)
    monkeypatch.setattr(work_cmd, "review_closeout", fake_review_closeout)

    assert cli.main(["work", "review", "init", "--target", str(tmp_path), "--force", "--no-gitignore"]) == 0
    assert cli.main(["work", "review", "plan", "--target", str(tmp_path), "--json"]) == 0
    assert (
        cli.main(
            [
                "work",
                "review",
                "run",
                "codex-review",
                "--target",
                str(tmp_path),
                "--include-disabled",
                "--json",
            ]
        )
        == 0
    )
    assert cli.main(["work", "review", "run", "--all", "--target", str(tmp_path)]) == 0
    assert cli.main(["work", "review", "runs", "--target", str(tmp_path), "--limit", "5", "--json"]) == 0
    assert cli.main(["work", "review", "show", "run-1", "--target", str(tmp_path), "--json"]) == 0
    assert (
        cli.main(["work", "review", "import-findings", "run-1", "--target", str(tmp_path), "--dry-run", "--json"]) == 0
    )
    assert cli.main(["work", "review", "findings", "--target", str(tmp_path), "--run-id", "run-1", "--json"]) == 0
    assert cli.main(["work", "review", "finding-show", "finding-one", "--target", str(tmp_path), "--json"]) == 0
    assert cli.main(["work", "review", "closeout", "latest", "--target", str(tmp_path), "--json"]) == 0

    assert seen == [
        ("init", {"target": tmp_path, "force": True, "update_gitignore": False}),
        ("plan", {"target": tmp_path, "json_output": True}),
        (
            "run",
            {
                "target": tmp_path,
                "reviewer_id": "codex-review",
                "all_matching": False,
                "include_disabled": True,
                "json_output": True,
            },
        ),
        (
            "run",
            {
                "target": tmp_path,
                "reviewer_id": None,
                "all_matching": True,
                "include_disabled": False,
                "json_output": False,
            },
        ),
        ("runs", {"target": tmp_path, "json_output": True, "limit": 5}),
        ("show", {"target": tmp_path, "run_id": "run-1", "json_output": True}),
        ("import-findings", {"target": tmp_path, "run_id": "run-1", "dry_run": True, "json_output": True}),
        ("findings", {"target": tmp_path, "run_id": "run-1", "json_output": True}),
        ("finding-show", {"target": tmp_path, "finding_id": "finding-one", "json_output": True}),
        ("closeout", {"target": tmp_path, "run_id": "latest", "json_output": True}),
    ]
