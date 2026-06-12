import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

from brigade import cli
from brigade import dogfood_cmd
from brigade import localio
from brigade import work_cmd

from tests.work_cmd_test_helpers import (
    _write_json,
    _init_git_repo,
)


def test_work_doctor_warns_for_scanner_queue_health(tmp_path, monkeypatch, capsys):
    _init_git_repo(tmp_path)
    dogfood_cmd.init(target=tmp_path)
    monkeypatch.setattr(work_cmd.helpers.shutil, "which", lambda name: "/usr/bin/codex" if name == "codex" else None)
    monkeypatch.setattr(localio, "check_git_ignored", lambda repo, path: "yes")
    monkeypatch.setattr(
        work_cmd.helpers,
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


def test_work_scanners_init_list_show_plan_and_json(tmp_path, monkeypatch, capsys):
    _init_git_repo(tmp_path)
    monkeypatch.setattr(localio, "check_git_ignored", lambda repo, path: "yes")

    assert work_cmd.scanners_init(target=tmp_path) == 0
    out = capsys.readouterr().out
    config = tmp_path / ".brigade" / "scanners.toml"
    assert f"scanner_config: {config}" in out
    assert "scanners: 8" in out
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
    monkeypatch.setattr(work_cmd.helpers.shutil, "which", lambda name: f"/usr/bin/{name}")
    monkeypatch.setattr(
        work_cmd.helpers,
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


def test_work_scanners_doctor_warns_for_missing_stale_bad_and_imports_issues(tmp_path, monkeypatch, capsys):
    _init_git_repo(tmp_path)
    monkeypatch.setattr(
        work_cmd.helpers,
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
    monkeypatch.setattr(work_cmd.helpers.shutil, "which", lambda name: f"/usr/bin/{name}")
    monkeypatch.setattr(localio, "check_git_ignored", lambda repo, path: "yes")
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
