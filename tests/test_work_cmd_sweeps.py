import json
import sys
from datetime import datetime, timezone
from pathlib import Path

from brigade import cli
from brigade import dogfood_cmd
from brigade import work_cmd

from tests.work_cmd_test_helpers import (
    _write_json,
    _init_git_repo,
    _write_chat_surfaces_config,
    _chat_finding,
)


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
        f'brigade work import dismiss {imports[0]["id"]} --reason "..."',
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
    assert any(
        run["scanner_id"] == "fail-scan" and run["status"] == "failed" for run in all_report["run_result"]["runs"]
    )


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
    monkeypatch.setattr(work_cmd.helpers.shutil, "which", lambda name: f"/usr/bin/{name}")
    monkeypatch.setattr(
        work_cmd.helpers,
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
    monkeypatch.setattr(work_cmd.helpers.shutil, "which", lambda name: f"/usr/bin/{name}")
    monkeypatch.setattr(
        work_cmd.helpers,
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


def test_chat_surfaces_integrate_with_work_brief_doctor_and_scanner_sweep(tmp_path, monkeypatch, capsys):
    _init_git_repo(tmp_path)
    dogfood_cmd.init(target=tmp_path)
    capsys.readouterr()
    monkeypatch.setattr(work_cmd.helpers.shutil, "which", lambda name: f"/usr/bin/{name}")
    export = tmp_path / ".brigade" / "chat-surfaces" / "discord.json"
    export.parent.mkdir(parents=True)
    _write_json(export, {"findings": [_chat_finding("discord-export", "discord-export")]})
    _write_chat_surfaces_config(
        tmp_path,
        [
            {
                "id": "discord-export",
                "provider": "discord-export",
                "workspace_label": "local-discord",
                "channel_label": "triage",
                "export_path": ".brigade/chat-surfaces/discord.json",
                "sweep_output_path": ".brigade/chat-memory-sweeps/discord-export-latest.json",
                "enabled": True,
                "privacy_mode": "summary-only",
                "evidence_policy": "local-path",
                "confidence_threshold": "medium",
            }
        ],
    )
    scanner = tmp_path / ".brigade" / "scanners.toml"
    runner = tmp_path / "chat_surface_runner.py"
    runner.write_text(
        f"""
import sys
from pathlib import Path
sys.path.insert(0, {str(Path(__file__).parents[1] / "src")!r})
from brigade import chat_cmd

raise SystemExit(chat_cmd.sweep_import_issues(target=Path("."), surface_id="discord-export", json_output=True))
"""
    )
    scanner.write_text(
        f"""
[[scanner]]
id = "chat-surfaces"
source = "chat-memory-sweep"
command = "{sys.executable} {runner}"
cadence = "daily@02:00"
enabled = true
timeout = 30
output_path = ".brigade/chat-memory-sweeps/discord-export-latest.json"
conflict_window = "02:00-02:10"
"""
    )

    assert work_cmd.sweep(target=tmp_path, ingest=False, json_output=True) == 0
    report = json.loads(capsys.readouterr().out)
    assert report["status"] == "completed"
    imports = work_cmd._read_imports(tmp_path)
    assert len(imports) == 1
    assert imports[0]["source"] == "chat-memory-sweep"
    assert imports[0]["metadata"]["scanner_id"] == "chat-surfaces"

    assert work_cmd.brief(target=tmp_path) == 0
    out = capsys.readouterr().out
    assert "chat_surfaces_health: ok" in out
    assert "scanner_next_source: chat-memory-sweep" in out

    assert work_cmd.doctor(target=tmp_path) == 0
    out = capsys.readouterr().out
    assert "[ok] chat_surfaces_config: 1 surface(s)" in out


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

    def fake_sweep_closeout(**kwargs):
        seen.append(("sweep-closeout", kwargs))
        return 0

    monkeypatch.setattr(work_cmd, "sweep", fake_sweep)
    monkeypatch.setattr(work_cmd, "sweeps", fake_sweeps)
    monkeypatch.setattr(work_cmd, "sweep_show", fake_sweep_show)
    monkeypatch.setattr(work_cmd, "sweep_review", fake_sweep_review)
    monkeypatch.setattr(work_cmd, "sweep_closeout", fake_sweep_closeout)

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
    assert (
        cli.main(
            [
                "work",
                "sweep",
                "closeout",
                "latest",
                "--target",
                str(tmp_path),
                "--defer",
                "import-one",
                "--reason",
                "reviewed",
                "--json",
            ]
        )
        == 0
    )
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
        (
            "sweep-closeout",
            {
                "target": tmp_path,
                "sweep_id": "latest",
                "reason": "reviewed",
                "deferred_imports": ["import-one"],
                "defer_all": False,
                "json_output": True,
            },
        ),
    ]


def test_sweep_closeout_blocks_pending_imports(tmp_path, capsys):
    tmp_path.mkdir(exist_ok=True)
    item = work_cmd._make_import(
        "Review scanner finding",
        kind="task",
        source="scanner-health",
        metadata={
            "scanner_id": "scanner-one",
            "scanner_source": "scanner-health",
            "scanner_run_id": "run-one",
            "source_fingerprint": "abc",
        },
    )
    work_cmd._write_imports(tmp_path, [item])
    work_cmd._write_sweep_report(
        tmp_path,
        {
            "sweep_id": "sweep-one",
            "status": "completed",
            "started_at": "2026-05-29T12:00:00+00:00",
            "completed_at": "2026-05-29T12:01:00+00:00",
            "import_references": {"created_import_ids": [item["id"]]},
        },
    )

    assert work_cmd.sweep_closeout(target=tmp_path, sweep_id="sweep-one", json_output=True) == 2
    payload = json.loads(capsys.readouterr().out)
    assert payload["closeout"]["status"] == "blocked"
    assert "pending imports remain unreviewed" in payload["closeout"]["blocked_reasons"]


def test_sweep_closeout_can_defer_pending_imports(tmp_path, capsys):
    tmp_path.mkdir(exist_ok=True)
    item = work_cmd._make_import(
        "Review scanner task",
        kind="task",
        source="scanner-health",
        metadata={
            "scanner_id": "scanner-one",
            "scanner_source": "scanner-health",
            "scanner_run_id": "run-two",
            "source_fingerprint": "def",
        },
    )
    work_cmd._write_imports(tmp_path, [item])
    work_cmd._write_sweep_report(
        tmp_path,
        {
            "sweep_id": "sweep-two",
            "status": "completed",
            "started_at": "2026-05-29T12:00:00+00:00",
            "completed_at": "2026-05-29T12:01:00+00:00",
            "import_references": {"created_import_ids": [item["id"]]},
        },
    )

    assert work_cmd.sweep_closeout(target=tmp_path, sweep_id="latest", defer_all=True, json_output=True) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["closeout"]["status"] == "reviewed_with_deferrals"

    report, error = work_cmd._find_sweep_report(tmp_path, "sweep-two")
    assert error is None
    assert report["review_closeout"]["deferred_import_ids"] == [item["id"]]
    review, error = work_cmd._sweep_review_payload(tmp_path, "sweep-two")
    assert error is None
    assert not review["issues"]


def test_sweep_closeout_missing_reference_is_blocked(tmp_path, capsys):
    tmp_path.mkdir(exist_ok=True)
    work_cmd._write_sweep_report(
        tmp_path,
        {
            "sweep_id": "sweep-missing",
            "status": "completed",
            "started_at": "2026-05-29T12:00:00+00:00",
            "completed_at": "2026-05-29T12:01:00+00:00",
            "import_references": {"created_import_ids": ["missing-import"]},
        },
    )

    assert work_cmd.sweep_closeout(target=tmp_path, sweep_id="sweep-missing", defer_all=True, json_output=True) == 2
    payload = json.loads(capsys.readouterr().out)
    assert payload["closeout"]["status"] == "blocked"
    assert payload["closeout"]["missing_import_ids"] == ["missing-import"]


def test_inbox_hygiene_reports_unclosed_sweeps(tmp_path):
    tmp_path.mkdir(exist_ok=True)
    item = work_cmd._make_import("Review scanner task", kind="task", source="scanner-health")
    work_cmd._write_imports(tmp_path, [item])
    work_cmd._write_sweep_report(
        tmp_path,
        {
            "sweep_id": "sweep-open",
            "status": "completed",
            "started_at": "2026-05-29T12:00:00+00:00",
            "completed_at": "2026-05-29T12:01:00+00:00",
            "import_references": {"created_import_ids": [item["id"]]},
        },
    )

    payload = work_cmd._inbox_hygiene_payload(tmp_path)
    issue_names = {issue["name"] for issue in payload["issues"]}
    assert "inbox_sweep_unclosed" in issue_names
