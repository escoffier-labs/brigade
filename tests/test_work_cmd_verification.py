import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from brigade import cli
from brigade import localio
from brigade import work_cmd

from tests.work_cmd_test_helpers import (
    _write_json,
    _init_git_repo,
)


def _init_git_repo_with_head(path):
    _init_git_repo(path)
    (path / ".gitignore").write_text(".brigade/\n")
    subprocess.run(["git", "add", ".gitignore"], cwd=path, check=True, stdout=subprocess.DEVNULL)
    subprocess.run(
        ["git", "-c", "user.name=Test User", "-c", "user.email=test@example.invalid", "commit", "-m", "init"],
        cwd=path,
        check=True,
        stdout=subprocess.DEVNULL,
    )


def test_verify_run_marks_parser_rejected_command_as_rejected_not_failed(tmp_path, capsys):
    # A command Brigade's own parser refuses (shell metacharacters here) never runs;
    # it is invalid input, not a verified regression, so the receipt status must be
    # 'rejected' (neutral for outcome capture), never 'failed' (-1).
    _init_git_repo(tmp_path)
    rc = work_cmd.verify_run(target=tmp_path, commands=["echo hi && echo bye"], json_output=True)
    payload = json.loads(capsys.readouterr().out)
    assert rc != 0
    assert payload["status"] == "rejected"
    assert payload["commands"][0]["status"] == "rejected"

    from brigade import outcome_cmd

    assert outcome_cmd.capture(target=tmp_path, artifact_id="brigade-work", json_output=True) == 0
    record = json.loads(capsys.readouterr().out)["record"]
    assert record["signal_value"] == 0


def test_verify_run_capture_records_outcome_in_one_step(tmp_path, capsys):
    _init_git_repo(tmp_path)
    from brigade import outcome_cmd

    rc = work_cmd.verify_run(
        target=tmp_path, commands=["python3 -c \"print('ok')\""], capture="skill-x", capture_kind="skill"
    )
    assert rc == 0
    capsys.readouterr()
    records = outcome_cmd.load_records(tmp_path)
    assert len(records) == 1
    assert records[0].artifact_id == "skill-x" and records[0].signal_value == 1


def test_prune_verify_runs_keeps_newest(tmp_path):
    from brigade.work_cmd import helpers, verification

    root = helpers._verify_runs_root(tmp_path)
    root.mkdir(parents=True)
    for name in ("20260101-000001-a", "20260101-000002-b", "20260101-000003-c"):
        (root / name).mkdir()
    removed = verification._prune_verify_runs(tmp_path, keep=2)
    assert removed == 1
    assert sorted(p.name for p in root.iterdir()) == ["20260101-000002-b", "20260101-000003-c"]


def test_outcome_health_flags_dormant_then_half_fed(tmp_path):
    from brigade import outcome_cmd

    dormant = outcome_cmd.health(tmp_path)
    assert dormant["record_count"] == 0 and dormant["verify_run_count"] == 0
    assert dormant["top_issue"]["name"] == "outcome_loop_dormant"

    _init_git_repo(tmp_path)
    assert work_cmd.verify_run(target=tmp_path, commands=["python3 -c \"print('ok')\""]) == 0
    half_fed = outcome_cmd.health(tmp_path)
    assert half_fed["verify_run_count"] >= 1 and half_fed["record_count"] == 0
    assert half_fed["top_issue"]["name"] == "outcome_loop_half_fed"


def test_work_acceptance_rollup_covers_completion_review_and_closeout(tmp_path, capsys):
    _init_git_repo(tmp_path)
    ledger = {
        "version": 1,
        "tasks": [
            {
                "id": "pending-ready",
                "text": "Pending with acceptance",
                "status": "pending",
                "acceptance": ["Ready acceptance."],
            },
            {
                "id": "pending-missing",
                "text": "Pending missing acceptance",
                "status": "pending",
            },
            {
                "id": "done-ready",
                "text": "Done with completion",
                "status": "done",
                "acceptance": ["Done acceptance."],
                "completed_acceptance": ["Done acceptance."],
                "completion": {"session_path": ".brigade/work/session-one"},
            },
            {
                "id": "done-missing-completion",
                "text": "Done missing completion",
                "status": "done",
                "acceptance": ["Done acceptance."],
                "completed_acceptance": ["Done acceptance."],
            },
            {
                "id": "done-missing-completed-acceptance",
                "text": "Done missing completed acceptance",
                "status": "done",
                "acceptance": ["Done acceptance."],
                "completion": {"session_path": ".brigade/work/session-two"},
            },
        ],
    }
    work_cmd._write_task_ledger(tmp_path, ledger)
    imports = []
    for finding_id, status, task_id, dismiss_reason in (
        ("pending-finding", "pending", None, None),
        ("dismissed-finding", "dismissed", None, "not actionable"),
        ("completed-finding", "promoted", "done-ready", None),
    ):
        item = work_cmd._make_import(
            f"Review finding {finding_id}",
            kind="task",
            source="code-review",
            metadata={
                "reviewer_id": "codex-review",
                "review_run_id": "run-one",
                "review_finding_id": finding_id,
                "source_item_key": f"code-review:codex-review:{finding_id}",
                "source_fingerprint": f"fp-{finding_id}",
            },
        )
        item["status"] = status
        if task_id:
            item["task_id"] = task_id
        if dismiss_reason:
            item["dismiss_reason"] = dismiss_reason
        imports.append(item)
    work_cmd._write_imports(tmp_path, imports)
    (tmp_path / ".brigade" / "work" / "closeouts" / "blocked-closeout").mkdir(parents=True)
    _write_json(
        tmp_path / ".brigade" / "work" / "closeouts" / "blocked-closeout" / "closeout.json",
        {
            "closeout_id": "blocked-closeout",
            "ready": False,
            "status": "blocked",
            "created_at": "2026-05-29T12:00:00+00:00",
            "acceptance_criteria": ["Closeout acceptance."],
            "blockers": ["review run is not closed out"],
        },
    )

    assert work_cmd.acceptance(target=tmp_path, json_output=True) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["pending_with_acceptance"] == ["pending-ready"]
    assert payload["pending_missing_acceptance"] == ["pending-missing"]
    assert payload["done_with_completion"] == ["done-ready", "done-missing-completed-acceptance"]
    assert payload["done_missing_completion"] == ["done-missing-completion"]
    assert payload["done_missing_completed_acceptance"] == ["done-missing-completed-acceptance"]
    assert payload["review_findings"]["outcomes"] == {
        "completed": 1,
        "dismissed": 1,
        "pending": 1,
    }
    assert payload["latest_work_closeout"]["closeout_id"] == "blocked-closeout"
    issue_names = {issue["name"] for issue in payload["issues"]}
    assert "acceptance_pending_missing" in issue_names
    assert "acceptance_done_missing_completion" in issue_names
    assert "acceptance_done_missing_completed_acceptance" in issue_names
    assert "acceptance_review_findings_unresolved" in issue_names
    assert "acceptance_work_closeout_blocked" in issue_names

    assert work_cmd.acceptance(target=tmp_path) == 0
    out = capsys.readouterr().out
    assert "done_missing_completed_acceptance: 1" in out
    assert "review_findings_unresolved: 1" in out
    assert "work_closeout: blocked-closeout" in out


def test_work_verify_plan_run_list_show(tmp_path, capsys):
    _init_git_repo(tmp_path)

    assert work_cmd.verify_plan(target=tmp_path, commands=["python3 -c \"print('ok')\""], json_output=True) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["commands"] == ["python3 -c \"print('ok')\""]
    assert payload["blockers"] == []

    assert (
        work_cmd.verify_run(target=tmp_path, commands=["python3 -c \"print('ok')\""], timeout=30, json_output=True) == 0
    )
    receipt = json.loads(capsys.readouterr().out)
    assert receipt["status"] == "completed"
    assert receipt["commands"][0]["stdout_summary"] == "ok"
    assert Path(receipt["commands"][0]["stdout_log_path"]).is_file()
    assert Path(receipt["path"], "receipt.json").is_file()
    assert Path(receipt["path"], "summary.md").is_file()

    assert work_cmd.verify_runs(target=tmp_path, json_output=True) == 0
    runs = json.loads(capsys.readouterr().out)
    assert runs["runs"][0]["run_id"] == receipt["run_id"]

    assert work_cmd.verify_show(target=tmp_path, run_id="latest") == 0
    out = capsys.readouterr().out
    assert f"work verify run: {receipt['run_id']}" in out
    assert "python3 -c" in out


def test_work_verify_run_argv_json_bypasses_metacharacter_heuristic(tmp_path, capsys):
    # A quoted argument containing shell metacharacters (semicolons, quotes) is safe
    # when it arrives as pre-parsed argv: shell=False was already the execution mode,
    # so the string-split heuristic is irrelevant and must not reject it.
    _init_git_repo(tmp_path)
    argv = ["python3", "-c", "print(1); print(2)"]

    rc = cli.main(
        [
            "work",
            "verify",
            "run",
            "--target",
            str(tmp_path),
            "--argv-json",
            json.dumps(argv),
            "--json",
        ]
    )
    receipt = json.loads(capsys.readouterr().out)
    assert rc == 0
    assert receipt["status"] == "completed"
    assert receipt["commands"][0]["status"] == "completed"
    assert receipt["commands"][0]["argv"] == argv
    assert receipt["commands"][0]["stdout_summary"] == "1\n2"
    assert Path(receipt["path"], "receipt.json").is_file()


def test_work_verify_run_command_still_rejects_shell_metacharacters(tmp_path, capsys):
    _init_git_repo(tmp_path)

    rc = cli.main(
        [
            "work",
            "verify",
            "run",
            "--target",
            str(tmp_path),
            "--command",
            "python3 -c \"print(1); print(2)\"",
            "--json",
        ]
    )
    receipt = json.loads(capsys.readouterr().out)
    assert rc != 0
    assert receipt["status"] == "rejected"
    assert receipt["commands"][0]["status"] == "rejected"
    assert "shell metacharacters" in receipt["commands"][0]["stderr_summary"]
    assert "--argv-json" in receipt["commands"][0]["stderr_summary"]


def test_work_verify_run_command_and_argv_json_are_mutually_exclusive(tmp_path, capsys):
    with pytest.raises(SystemExit) as exc:
        cli.main(
            [
                "work",
                "verify",
                "run",
                "--target",
                str(tmp_path),
                "--command",
                "python3 -m pytest -q",
                "--argv-json",
                json.dumps(["python3", "-m", "pytest", "-q"]),
            ]
        )
    assert exc.value.code == 2
    err = capsys.readouterr().err
    assert "--command" in err and "--argv-json" in err
    assert "mutually exclusive" in err


def test_work_verify_run_requires_exactly_one_of_command_or_argv_json(tmp_path, capsys):
    with pytest.raises(SystemExit) as exc:
        cli.main(["work", "verify", "run", "--target", str(tmp_path)])
    assert exc.value.code == 2
    err = capsys.readouterr().err
    assert "--command" in err and "--argv-json" in err


def test_work_verify_run_argv_json_rejects_malformed_json(tmp_path, capsys):
    with pytest.raises(SystemExit) as exc:
        cli.main(["work", "verify", "run", "--target", str(tmp_path), "--argv-json", "not-json"])
    assert exc.value.code == 2
    err = capsys.readouterr().err
    assert "--argv-json" in err

    with pytest.raises(SystemExit) as exc:
        cli.main(["work", "verify", "run", "--target", str(tmp_path), "--argv-json", json.dumps({"not": "an array"})])
    assert exc.value.code == 2
    err = capsys.readouterr().err
    assert "--argv-json" in err


def test_work_verify_receipt_digests_recompute_from_payload_and_logs(tmp_path, capsys, monkeypatch):
    _init_git_repo(tmp_path)
    monkeypatch.setenv("GRAPHTRAIL_BIN", str(tmp_path / "missing-graphtrail"))
    monkeypatch.setenv("PATH", str(tmp_path))
    monkeypatch.setenv("HOME", str(tmp_path))

    assert (
        work_cmd.verify_run(
            target=tmp_path,
            commands=[f"{sys.executable} -c \"print('ok')\""],
            timeout=30,
            json_output=True,
        )
        == 0
    )
    receipt = json.loads(capsys.readouterr().out)
    digests = receipt["digests"]
    run_dir = Path(receipt["path"])

    assert digests["algorithm"] == "sha256"
    assert digests["receipt_sha256"] == localio.canonical_json_digest(receipt, exclude_keys={"digests"})
    assert digests["logs"] == {
        "command-1-stderr.log": localio.file_sha256(run_dir / "command-1-stderr.log"),
        "command-1-stdout.log": localio.file_sha256(run_dir / "command-1-stdout.log"),
    }

    stored = json.loads((run_dir / "receipt.json").read_text())
    assert stored["digests"] == digests


def test_work_verify_receipt_captures_git_state_before_digest(tmp_path, capsys, monkeypatch):
    _init_git_repo_with_head(tmp_path)
    (tmp_path / "dirty.txt").write_text("dirty\n")
    monkeypatch.setenv("GRAPHTRAIL_BIN", str(tmp_path / "missing-graphtrail"))
    monkeypatch.setenv("HOME", str(tmp_path))

    assert (
        work_cmd.verify_run(
            target=tmp_path,
            commands=[f"{sys.executable} -c \"print('ok')\""],
            timeout=30,
            json_output=True,
        )
        == 0
    )
    receipt = json.loads(capsys.readouterr().out)
    dirty_files = subprocess.check_output(["git", "-C", str(tmp_path), "status", "--porcelain"], text=True)

    assert receipt["git"] == {
        "head": subprocess.check_output(["git", "-C", str(tmp_path), "rev-parse", "HEAD"], text=True).strip(),
        "branch": subprocess.check_output(
            ["git", "-C", str(tmp_path), "rev-parse", "--abbrev-ref", "HEAD"], text=True
        ).strip(),
        "dirty_files": len(dirty_files.splitlines()),
    }
    assert receipt["digests"]["receipt_sha256"] == localio.canonical_json_digest(receipt, exclude_keys={"digests"})


def test_work_verify_receipt_omits_git_state_outside_git_repo(tmp_path, capsys, monkeypatch):
    monkeypatch.setenv("GRAPHTRAIL_BIN", str(tmp_path / "missing-graphtrail"))
    monkeypatch.setenv("HOME", str(tmp_path))

    assert (
        work_cmd.verify_run(
            target=tmp_path,
            commands=[f"{sys.executable} -c \"print('ok')\""],
            timeout=30,
            json_output=True,
        )
        == 0
    )
    receipt = json.loads(capsys.readouterr().out)

    assert "git" not in receipt
    assert receipt["digests"]["receipt_sha256"] == localio.canonical_json_digest(receipt, exclude_keys={"digests"})


def _write_fake_graphtrail(tmp_path, *, mode: str = "ok") -> Path:
    script = tmp_path / "fake-graphtrail.py"
    script.write_text(
        """
import hashlib
import json
import os
import sqlite3
import sys
from pathlib import Path

args = sys.argv[1:]
db = Path(args[args.index("--db") + 1]) if "--db" in args else Path(".graphtrail/graphtrail.db")
command = args[args.index(str(db)) + 1] if str(db) in args and args.index(str(db)) + 1 < len(args) else ""
mode = os.environ.get("FAKE_GRAPHTRAIL_MODE", "ok")

# Mirror the real clap CLI strictly: `sync` rejects --json, `diff` requires
# --before/--after/--json. JSON shape follows graphtrail's diff golden fixture.
if command == "sync":
    if "--json" in args:
        print("error: unexpected argument '--json' found", file=sys.stderr)
        raise SystemExit(2)
    if mode == "sync-fail":
        print("sync failed", file=sys.stderr)
        raise SystemExit(5)
    db.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(db) as con:
        con.execute("create table if not exists symbols (name text)")
        con.execute("insert into symbols values ('after')")
    print("indexed files=1 symbols=1 calls=0 imports=0 deleted=0 db=" + str(db))
    raise SystemExit(0)

if command == "diff":
    if "--before" not in args or "--after" not in args or "--json" not in args:
        print("error: required arguments missing (--before/--after/--json)", file=sys.stderr)
        raise SystemExit(2)
    before = Path(args[args.index("--before") + 1])
    after = Path(args[args.index("--after") + 1])
    if not before.is_file() or not after.is_file():
        print("error: no such database", file=sys.stderr)
        raise SystemExit(1)
    if mode == "malformed-diff":
        print("{not-json")
        raise SystemExit(0)

    def node(name, line=1):
        return {
            "kind": "function",
            "qualified_name": name,
            "file_path": "pkg/mod.py",
            "start_line": line,
            "signature": "def " + name + "()",
        }

    def edge(source, target, line):
        return {
            "source_file": "pkg/a.py",
            "source": source,
            "line": line,
            "target_file": "pkg/b.py",
            "target": target,
        }

    payload = {
        "schema_version": 3,
        "summary": {
            "added_nodes": 2,
            "removed_nodes": 1,
            "changed_nodes": 25,
            "added_edges": 2,
            "removed_edges": 1,
        },
        "added_nodes": [node("pkg.new_a"), node("pkg.new_b")],
        "removed_nodes": [node("pkg.gone")],
        "changed_nodes": [node(f"pkg.symbol_{i}", line=i + 1) for i in range(25)],
        "added_edges": [edge("pkg.a", "pkg.b", 20), edge("pkg.c", "pkg.d", 30)],
        "removed_edges": [edge("pkg.a", "pkg.b", 10)],
    }
    print(json.dumps(payload))
    raise SystemExit(0)

print("unexpected command: " + repr(args), file=sys.stderr)
raise SystemExit(9)
"""
    )
    script.chmod(0o755)
    wrapper = tmp_path / "graphtrail"
    wrapper.write_text(
        f'#!/bin/sh\nFAKE_GRAPHTRAIL_MODE={mode} exec {os.environ.get("PYTHON", "python3")} {script} "$@"\n'
    )
    wrapper.chmod(0o755)
    return wrapper


def test_work_verify_graphtrail_delta_missing_binary_fails_open(tmp_path, capsys, monkeypatch):
    _init_git_repo(tmp_path)
    monkeypatch.setenv("GRAPHTRAIL_BIN", str(tmp_path / "missing-graphtrail"))
    monkeypatch.setenv("PATH", str(tmp_path))
    monkeypatch.setenv("HOME", str(tmp_path))

    assert (
        work_cmd.verify_run(target=tmp_path, commands=[f"{sys.executable} -c \"print('ok')\""], json_output=True) == 0
    )
    receipt = json.loads(capsys.readouterr().out)

    delta = receipt["code_graph_delta"]
    assert delta["status"] == "unavailable"
    assert "graphtrail binary not found" in delta["summary"]
    assert not (Path(receipt["path"]) / "graph-delta.json").exists()


def test_work_verify_graphtrail_delta_sidecar_digest_cleanup_and_summary(tmp_path, capsys, monkeypatch):
    _init_git_repo(tmp_path)
    graphtrail = _write_fake_graphtrail(tmp_path)
    monkeypatch.setenv("GRAPHTRAIL_BIN", str(graphtrail))

    assert work_cmd.verify_run(target=tmp_path, commands=["python3 -c \"print('ok')\""], json_output=True) == 0
    receipt = json.loads(capsys.readouterr().out)
    run_dir = Path(receipt["path"])
    sidecar_path = run_dir / "graph-delta.json"
    sidecar = json.loads(sidecar_path.read_text())

    assert receipt["code_graph_delta"]["status"] == "ok"
    assert receipt["code_graph_delta"]["edge_churn"] == 1
    assert receipt["code_graph_delta"]["changed_symbol_count"] == 20
    assert "edge_churn=1" in receipt["code_graph_delta"]["summary"]
    assert sidecar["raw_counts"] == {
        "added_nodes": 2,
        "removed_nodes": 1,
        "changed_nodes": 25,
        "added_edges": 2,
        "removed_edges": 1,
    }
    assert len(sidecar["changed_symbols"]) == 20
    assert sidecar["changed_symbols_truncated"] is True
    assert sidecar["edge_churn"] == 1
    assert sidecar["snapshot_deleted"] is True
    assert not Path(sidecar["before_snapshot_path"]).exists()
    assert not (run_dir / "graphtrail-after.db").exists()
    assert sidecar["attestations"]["before_snapshot_sha256"]
    after_sha = sidecar["attestations"]["after_snapshot_sha256"]
    assert isinstance(after_sha, str) and len(after_sha) == 64
    assert receipt["digests"]["logs"]["graph-delta.json"] == localio.file_sha256(sidecar_path)
    assert json.loads((run_dir / "receipt.json").read_text())["digests"] == receipt["digests"]
    assert "- Code graph delta: " + receipt["code_graph_delta"]["summary"] in (run_dir / "summary.md").read_text()


def test_work_verify_graphtrail_delta_sync_failure_fails_open(tmp_path, capsys, monkeypatch):
    _init_git_repo(tmp_path)
    graphtrail = _write_fake_graphtrail(tmp_path, mode="sync-fail")
    monkeypatch.setenv("GRAPHTRAIL_BIN", str(graphtrail))

    assert work_cmd.verify_run(target=tmp_path, commands=["python3 -c \"print('ok')\""], json_output=True) == 0
    receipt = json.loads(capsys.readouterr().out)
    sidecar = json.loads((Path(receipt["path"]) / "graph-delta.json").read_text())

    assert receipt["code_graph_delta"]["status"] == "sync_failed"
    assert sidecar["status"] == "sync_failed"
    assert sidecar["ok"] is False


def test_work_verify_graphtrail_delta_malformed_diff_fails_open(tmp_path, capsys, monkeypatch):
    _init_git_repo(tmp_path)
    graphtrail = _write_fake_graphtrail(tmp_path, mode="malformed-diff")
    monkeypatch.setenv("GRAPHTRAIL_BIN", str(graphtrail))

    assert work_cmd.verify_run(target=tmp_path, commands=["python3 -c \"print('ok')\""], json_output=True) == 0
    receipt = json.loads(capsys.readouterr().out)
    sidecar = json.loads((Path(receipt["path"]) / "graph-delta.json").read_text())

    assert receipt["code_graph_delta"]["status"] == "diff_malformed"
    assert sidecar["status"] == "diff_malformed"
    assert sidecar["ok"] is False


def test_work_closeout_writes_ready_receipt(tmp_path, capsys):
    _init_git_repo(tmp_path)
    task = {
        "id": "task-one",
        "text": "Ship feature",
        "source": "manual",
        "type": "feature",
        "priority": "normal",
        "acceptance": ["Tests pass."],
    }
    assert work_cmd.start(target=tmp_path, title="Ship feature", force=False, task_snapshot=task) == 0
    capsys.readouterr()
    assert work_cmd.end(target=tmp_path, note="done", handoff=False) == 0
    capsys.readouterr()
    assert work_cmd.verify_run(target=tmp_path, commands=["python3 -c \"print('verified')\""], timeout=30) == 0
    capsys.readouterr()

    assert work_cmd.closeout(target=tmp_path, session_id="latest", json_output=True) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["ready"] is True
    assert payload["status"] == "ready"
    assert payload["acceptance_criteria"] == ["Tests pass."]
    assert payload["verification"]["status"] == "completed"
    assert Path(payload["path"]).is_file()
    assert Path(payload["path"]).with_name("closeout.md").is_file()
    session = json.loads((Path(payload["session_path"]) / "session.json").read_text())
    assert session["closeout"]["closeout_id"] == payload["closeout_id"]


def test_work_closeout_blocks_failed_verification(tmp_path, capsys):
    _init_git_repo(tmp_path)
    task = {
        "id": "task-one",
        "text": "Ship feature",
        "source": "manual",
        "type": "feature",
        "priority": "normal",
        "acceptance": ["Tests pass."],
    }
    assert work_cmd.start(target=tmp_path, title="Ship feature", force=False, task_snapshot=task) == 0
    capsys.readouterr()
    assert work_cmd.end(target=tmp_path, note="done", handoff=False) == 0
    capsys.readouterr()
    assert work_cmd.verify_run(target=tmp_path, commands=['python3 -c "raise SystemExit(3)"'], timeout=30) == 3
    capsys.readouterr()

    assert work_cmd.closeout(target=tmp_path, session_id="latest", json_output=True) == 1
    payload = json.loads(capsys.readouterr().out)
    assert payload["ready"] is False
    assert payload["status"] == "blocked"
    assert "latest verification did not complete" in payload["blockers"][0]


def test_work_verify_and_closeout_cli(tmp_path, monkeypatch):
    seen = []

    def fake_verify_plan(**kwargs):
        seen.append(("verify-plan", kwargs))
        return 0

    def fake_verify_run(**kwargs):
        seen.append(("verify-run", kwargs))
        return 0

    def fake_verify_runs(**kwargs):
        seen.append(("verify-runs", kwargs))
        return 0

    def fake_verify_show(**kwargs):
        seen.append(("verify-show", kwargs))
        return 0

    def fake_closeout(**kwargs):
        seen.append(("closeout", kwargs))
        return 0

    monkeypatch.setattr(work_cmd, "verify_plan", fake_verify_plan)
    monkeypatch.setattr(work_cmd, "verify_run", fake_verify_run)
    monkeypatch.setattr(work_cmd, "verify_runs", fake_verify_runs)
    monkeypatch.setattr(work_cmd, "verify_show", fake_verify_show)
    monkeypatch.setattr(work_cmd, "closeout", fake_closeout)

    assert (
        cli.main(["work", "verify", "plan", "--target", str(tmp_path), "--command", "python3 -m pytest -q", "--json"])
        == 0
    )
    assert (
        cli.main(
            [
                "work",
                "verify",
                "run",
                "--target",
                str(tmp_path),
                "--command",
                "python3 -m pytest -q",
                "--timeout",
                "12",
                "--json",
            ]
        )
        == 0
    )
    assert cli.main(["work", "verify", "runs", "--target", str(tmp_path), "--limit", "3", "--json"]) == 0
    assert cli.main(["work", "verify", "show", "latest", "--target", str(tmp_path), "--json"]) == 0
    assert cli.main(["work", "closeout", "latest", "--target", str(tmp_path), "--json"]) == 0
    assert seen == [
        ("verify-plan", {"target": tmp_path, "commands": ["python3 -m pytest -q"], "json_output": True}),
        (
            "verify-run",
            {
                "target": tmp_path,
                "commands": ["python3 -m pytest -q"],
                "timeout": 12,
                "json_output": True,
                "capture": None,
                "capture_kind": "skill",
            },
        ),
        ("verify-runs", {"target": tmp_path, "limit": 3, "json_output": True}),
        ("verify-show", {"target": tmp_path, "run_id": "latest", "json_output": True}),
        ("closeout", {"target": tmp_path, "session_id": "latest", "json_output": True}),
    ]
