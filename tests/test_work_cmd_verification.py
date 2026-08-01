import hashlib
import json
import os
import shlex
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

from brigade import cli
from brigade import graphtrail_delta
from brigade import localio
from brigade import receipts_cmd
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
    captured = capsys.readouterr()
    assert "miseledger indexing:" in captured.out
    records = outcome_cmd.load_records(tmp_path)
    assert len(records) == 1
    assert records[0].artifact_id == "skill-x" and records[0].signal_value == 1


def _write_fake_miseledger_for_verify(path, marker, *, exit_code=0):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        f"""#!{sys.executable}
import json
import pathlib
import sys

assert sys.argv[1:3] == ["import", "adapter"]
export_path = pathlib.Path(sys.argv[3])
assert sys.argv[4:] == ["--source", "brigade", "--json"]
marker = pathlib.Path({str(marker)!r})
marker.write_text(export_path.read_text())
print(json.dumps({{"inserted_items": 1, "already_known": 0}}))
sys.exit({exit_code})
"""
    )
    path.chmod(0o755)


def test_verify_run_capture_auto_indexes_miseledger_receipts(tmp_path, monkeypatch, capsys):
    _init_git_repo(tmp_path)
    marker = tmp_path / "imported.jsonl"
    _write_fake_miseledger_for_verify(tmp_path / "bin" / "miseledger", marker)
    monkeypatch.setenv("PATH", f"{tmp_path / 'bin'}{os.pathsep}{os.environ['PATH']}")

    rc = work_cmd.verify_run(
        target=tmp_path,
        commands=["python3 -c \"print('ok')\""],
        capture="skill-x",
        capture_kind="skill",
    )
    captured = capsys.readouterr()

    assert rc == 0
    assert "miseledger indexing: indexed" in captured.out
    assert marker.is_file()
    cursor_path = tmp_path / ".brigade" / "work" / "miseledger-export-cursor.json"
    assert cursor_path.is_file()


def test_verify_run_capture_json_includes_miseledger_indexing_status(tmp_path, monkeypatch, capsys):
    _init_git_repo(tmp_path)
    marker = tmp_path / "imported.jsonl"
    _write_fake_miseledger_for_verify(tmp_path / "bin" / "miseledger", marker)
    monkeypatch.setenv("PATH", f"{tmp_path / 'bin'}{os.pathsep}{os.environ['PATH']}")

    rc = work_cmd.verify_run(
        target=tmp_path,
        commands=["python3 -c \"print('ok')\""],
        capture="skill-x",
        json_output=True,
    )
    captured = capsys.readouterr()

    assert rc == 0
    payload = json.loads(captured.out)
    indexing = payload["miseledger_indexing"]
    assert indexing["schema"] == "brigade.miseledger_index_result.v1"
    assert indexing["status"] == "indexed"
    assert indexing["exported_count"] >= 1
    assert captured.err == ""


def test_verify_run_capture_miseledger_failure_is_fail_open(tmp_path, monkeypatch, capsys):
    _init_git_repo(tmp_path)
    empty_path = tmp_path / "empty-path"
    empty_path.mkdir()
    monkeypatch.setenv("PATH", f"{empty_path}{os.pathsep}{os.environ['PATH']}")

    rc = work_cmd.verify_run(
        target=tmp_path,
        commands=["python3 -c \"print('ok')\""],
        capture="skill-x",
    )
    captured = capsys.readouterr()

    assert rc == 0
    assert "miseledger indexing: import failed" in captured.out
    cursor_path = tmp_path / ".brigade" / "work" / "miseledger-export-cursor.json"
    assert not cursor_path.exists()


def test_verify_run_capture_json_miseledger_failure_preserves_exit_code(tmp_path, monkeypatch, capsys):
    _init_git_repo(tmp_path)
    empty_path = tmp_path / "empty-path"
    empty_path.mkdir()
    monkeypatch.setenv("PATH", f"{empty_path}{os.pathsep}{os.environ['PATH']}")

    rc = work_cmd.verify_run(
        target=tmp_path,
        commands=['python3 -c "raise SystemExit(3)"'],
        capture="skill-x",
        json_output=True,
    )
    captured = capsys.readouterr()

    assert rc == 3
    payload = json.loads(captured.out)
    assert payload["miseledger_indexing"]["status"] == "failed"
    assert captured.err == ""


def test_verify_run_stamps_valid_claude_session_fingerprint(tmp_path, capsys, monkeypatch):
    from brigade.claude_hooks.runtime import _session_fingerprint

    _init_git_repo(tmp_path)
    fingerprint = _session_fingerprint("session-from-runtime")
    monkeypatch.setenv("BRIGADE_CLAUDE_SESSION", fingerprint)

    assert work_cmd.verify_run(target=tmp_path, commands=["python3 -c \"print('ok')\""], json_output=True) == 0

    receipt = json.loads(capsys.readouterr().out)
    assert receipt["harness_session"] == {"harness": "claude", "fingerprint": fingerprint}


def test_prune_verify_runs_keeps_newest(tmp_path):
    from brigade.work_cmd import helpers, verification

    root = helpers._verify_runs_root(tmp_path)
    root.mkdir(parents=True)
    for name in ("20260101-000001-a", "20260101-000002-b", "20260101-000003-c"):
        (root / name).mkdir()
    removed = verification._prune_verify_runs(tmp_path, keep=2)
    assert removed == 1
    assert sorted(p.name for p in root.iterdir()) == ["20260101-000002-b", "20260101-000003-c"]


def _write_verify_run_dir(root, name, *, schema_version=2, sign=False, tamper=False):
    """Build a run dir whose receipt carries a self-consistent digests block."""
    run_dir = root / name
    run_dir.mkdir(parents=True)
    log_path = run_dir / "command-1-stdout.log"
    log_path.write_text(f"stdout for {name}\n")
    receipt = {
        "schema_version": schema_version,
        "run_id": name,
        "target": str(root),
        "status": "completed",
        "started_at": "2026-01-01T00:00:00+00:00",
        "completed_at": "2026-01-01T00:00:01+00:00",
        "path": str(run_dir),
        "commands": [],
    }
    digests = {
        "algorithm": "sha256",
        "logs": {"command-1-stdout.log": localio.file_sha256(log_path)},
        "receipt_sha256": localio.canonical_json_digest(receipt, exclude_keys={"digests"}),
    }
    if sign:
        digests["signature"] = f"sig-{name}"
        digests["key_id"] = "test-key"
    if tamper:
        digests["receipt_sha256"] = "0" * 64
    receipt["digests"] = digests
    (run_dir / "receipt.json").write_text(json.dumps(receipt, indent=2, sort_keys=True) + "\n")
    return run_dir, receipt


def _read_archive_index(archive_root):
    return localio.read_jsonl_dicts(archive_root / "index.jsonl")


def test_prune_verify_runs_archives_evidence_before_delete(tmp_path):
    from brigade.work_cmd import helpers, verification

    root = helpers._verify_runs_root(tmp_path)
    root.mkdir(parents=True)
    names = ["20260101-000001-a", "20260101-000002-b", "20260101-000003-c", "20260101-000004-d"]
    source_bytes = {}
    receipts = {}
    for name in names:
        _, receipt = _write_verify_run_dir(root, name, sign=True)
        receipts[name] = receipt
        source_bytes[name] = (root / name / "receipt.json").read_bytes()

    archive_root = tmp_path / "verify-archive"
    removed = verification._prune_verify_runs(tmp_path, keep=2, archive_root=archive_root)

    assert removed == 2
    assert sorted(p.name for p in root.iterdir()) == ["20260101-000003-c", "20260101-000004-d"]
    # Evidence for the pruned runs survives in the archive, byte-identical.
    for name in ("20260101-000001-a", "20260101-000002-b"):
        archived = archive_root / name
        assert (archived / "receipt.json").read_bytes() == source_bytes[name]
        assert (archived / "command-1-stdout.log").is_file()
    # The append-only index records one line per archived run, oldest first,
    # carrying the receipt's integrity metadata and schema version.
    entries = _read_archive_index(archive_root)
    assert [entry["run_id"] for entry in entries] == ["20260101-000001-a", "20260101-000002-b"]
    for entry in entries:
        receipt = receipts[entry["run_id"]]
        assert entry["schema"] == "brigade.verify_archive_index.v1"
        assert entry["schema_version"] == 1
        assert entry["already_archived"] is False
        assert entry["receipt_schema_version"] == 2
        assert entry["receipt_sha256"] == receipt["digests"]["receipt_sha256"]
        assert entry["signature"] == receipt["digests"]["signature"]
        assert entry["key_id"] == "test-key"
        assert entry["receipt_file_sha256"] == hashlib.sha256(source_bytes[entry["run_id"]]).hexdigest()
        assert entry["status"] == "completed"
    # Archived receipts still verify against their own integrity metadata.
    for name in ("20260101-000001-a", "20260101-000002-b"):
        payload = json.loads((archive_root / name / "receipt.json").read_text())
        assert localio.canonical_json_digest(payload, exclude_keys={"digests"}) == payload["digests"]["receipt_sha256"]


def test_prune_verify_runs_archive_failure_keeps_run_dir(tmp_path, monkeypatch):
    from brigade.work_cmd import helpers, verification

    root = helpers._verify_runs_root(tmp_path)
    root.mkdir(parents=True)
    names = ["20260101-000001-a", "20260101-000002-b", "20260101-000003-c"]
    for name in names:
        _write_verify_run_dir(root, name)

    def _failing_archive(run_dir, archive_root):
        raise OSError("archive destination unavailable")

    monkeypatch.setattr(verification, "_archive_verify_run", _failing_archive)
    removed = verification._prune_verify_runs(tmp_path, keep=1, archive_root=tmp_path / "verify-archive")

    assert removed == 0
    assert sorted(p.name for p in root.iterdir()) == names


def test_prune_verify_runs_invalid_archive_config_keeps_run_dir(tmp_path):
    from brigade.work_cmd import helpers, verification

    root = helpers._verify_runs_root(tmp_path)
    root.mkdir(parents=True)
    _write_verify_run_dir(root, "20260101-000001-a")
    _write_verify_run_dir(root, "20260101-000002-b")
    _write_verify_retention_config(tmp_path, verify_archive_dir="")

    removed = verification._prune_verify_runs(tmp_path, keep=1)

    assert removed == 0
    assert (root / "20260101-000001-a" / "receipt.json").is_file()


@pytest.mark.parametrize("archive_location", ["equal", "ancestor", "descendant", "symlink-alias"])
def test_prune_verify_runs_rejects_archive_overlap(tmp_path, archive_location):
    from brigade.work_cmd import helpers, verification

    root = helpers._verify_runs_root(tmp_path)
    root.mkdir(parents=True)
    _write_verify_run_dir(root, "20260101-000001-a")
    _write_verify_run_dir(root, "20260101-000002-b")
    if archive_location == "equal":
        archive_root = root
    elif archive_location == "ancestor":
        archive_root = root.parent
    elif archive_location == "descendant":
        archive_root = root / "archive"
    else:
        archive_root = tmp_path / "archive-alias"
        archive_root.symlink_to(root, target_is_directory=True)

    removed = verification._prune_verify_runs(tmp_path, keep=1, archive_root=archive_root)

    assert removed == 0
    assert (root / "20260101-000001-a" / "receipt.json").is_file()


def test_prune_verify_runs_tampered_receipt_keeps_run_dir(tmp_path):
    from brigade.work_cmd import helpers, verification

    root = helpers._verify_runs_root(tmp_path)
    root.mkdir(parents=True)
    _write_verify_run_dir(root, "20260101-000001-a", tamper=True)
    _write_verify_run_dir(root, "20260101-000002-b")

    archive_root = tmp_path / "verify-archive"
    removed = verification._prune_verify_runs(tmp_path, keep=1, archive_root=archive_root)

    # The tampered receipt fails the post-copy integrity re-check, so its run
    # dir is kept locally and no archive copy is left behind.
    assert removed == 0
    assert sorted(p.name for p in root.iterdir()) == ["20260101-000001-a", "20260101-000002-b"]
    assert not (archive_root / "20260101-000001-a").exists()
    assert _read_archive_index(archive_root) == []


def test_prune_verify_runs_archive_conflict_keeps_run_dir(tmp_path):
    from brigade.work_cmd import helpers, verification

    root = helpers._verify_runs_root(tmp_path)
    root.mkdir(parents=True)
    _write_verify_run_dir(root, "20260101-000001-a")
    _write_verify_run_dir(root, "20260101-000002-b")

    archive_root = tmp_path / "verify-archive"
    conflicting = archive_root / "20260101-000001-a"
    conflicting.mkdir(parents=True)
    (conflicting / "receipt.json").write_text('{"run_id": "different-evidence"}\n')

    removed = verification._prune_verify_runs(tmp_path, keep=1, archive_root=archive_root)

    assert removed == 0
    assert (root / "20260101-000001-a").is_dir()


def test_prune_verify_runs_partial_existing_archive_keeps_run_dir(tmp_path):
    from brigade.work_cmd import helpers, verification

    root = helpers._verify_runs_root(tmp_path)
    root.mkdir(parents=True)
    run_dir, _ = _write_verify_run_dir(root, "20260101-000001-a")
    _write_verify_run_dir(root, "20260101-000002-b")

    archive_root = tmp_path / "verify-archive"
    archived = archive_root / run_dir.name
    archived.mkdir(parents=True)
    (archived / "receipt.json").write_bytes((run_dir / "receipt.json").read_bytes())

    removed = verification._prune_verify_runs(tmp_path, keep=1, archive_root=archive_root)

    assert removed == 0
    assert run_dir.is_dir()
    assert not (archived / "command-1-stdout.log").exists()


def test_prune_verify_runs_symlink_existing_archive_keeps_run_dir(tmp_path):
    from brigade.work_cmd import helpers, verification

    root = helpers._verify_runs_root(tmp_path)
    root.mkdir(parents=True)
    run_dir, _ = _write_verify_run_dir(root, "20260101-000001-a")
    _write_verify_run_dir(root, "20260101-000002-b")

    archive_root = tmp_path / "verify-archive"
    archive_root.mkdir()
    (archive_root / run_dir.name).symlink_to(run_dir, target_is_directory=True)

    removed = verification._prune_verify_runs(tmp_path, keep=1, archive_root=archive_root)

    assert removed == 0
    assert run_dir.is_dir()
    assert (run_dir / "receipt.json").is_file()


def test_prune_verify_runs_rearchive_identical_evidence_is_safe(tmp_path):
    from brigade.work_cmd import helpers, verification

    root = helpers._verify_runs_root(tmp_path)
    root.mkdir(parents=True)
    run_dir, receipt = _write_verify_run_dir(root, "20260101-000001-a")
    _write_verify_run_dir(root, "20260101-000002-b")

    archive_root = tmp_path / "verify-archive"
    import shutil as _shutil

    _shutil.copytree(run_dir, archive_root / "20260101-000001-a")

    removed = verification._prune_verify_runs(tmp_path, keep=1, archive_root=archive_root)

    assert removed == 1
    assert not (root / "20260101-000001-a").exists()
    entries = _read_archive_index(archive_root)
    assert len(entries) == 1
    assert entries[0]["already_archived"] is True
    assert entries[0]["receipt_sha256"] == receipt["digests"]["receipt_sha256"]


def test_prune_verify_runs_source_symlink_keeps_run_dir(tmp_path):
    from brigade.work_cmd import helpers, verification

    root = helpers._verify_runs_root(tmp_path)
    root.mkdir(parents=True)
    run_dir, _ = _write_verify_run_dir(root, "20260101-000001-a")
    _write_verify_run_dir(root, "20260101-000002-b")
    outside = tmp_path / "private.log"
    outside.write_text("private evidence\n")
    (run_dir / "linked.log").symlink_to(outside)

    archive_root = tmp_path / "verify-archive"
    removed = verification._prune_verify_runs(tmp_path, keep=1, archive_root=archive_root)

    assert removed == 0
    assert run_dir.is_dir()
    assert not (archive_root / run_dir.name).exists()


@pytest.mark.skipif(os.name != "posix", reason="requires POSIX FIFO support")
def test_prune_verify_runs_source_special_file_keeps_run_dir(tmp_path, monkeypatch):
    from brigade.work_cmd import helpers, verification

    root = helpers._verify_runs_root(tmp_path)
    root.mkdir(parents=True)
    run_dir, _ = _write_verify_run_dir(root, "20260101-000001-a")
    _write_verify_run_dir(root, "20260101-000002-b")
    os.mkfifo(run_dir / "stream")
    copy_attempted = False

    def _unexpected_copytree(*args, **kwargs):
        nonlocal copy_attempted
        copy_attempted = True
        raise OSError("copytree must not receive special files")

    monkeypatch.setattr(verification.shutil, "copytree", _unexpected_copytree)
    removed = verification._prune_verify_runs(tmp_path, keep=1, archive_root=tmp_path / "verify-archive")

    assert removed == 0
    assert copy_attempted is False
    assert run_dir.is_dir()


def test_prune_verify_runs_without_receipt_archives_with_null_metadata(tmp_path):
    from brigade.work_cmd import helpers, verification

    root = helpers._verify_runs_root(tmp_path)
    root.mkdir(parents=True)
    legacy = root / "20260101-000001-a"
    legacy.mkdir()
    (legacy / "command-1-stdout.log").write_text("partial run\n")
    _write_verify_run_dir(root, "20260101-000002-b")

    archive_root = tmp_path / "verify-archive"
    removed = verification._prune_verify_runs(tmp_path, keep=1, archive_root=archive_root)

    assert removed == 1
    assert (archive_root / "20260101-000001-a" / "command-1-stdout.log").is_file()
    entries = _read_archive_index(archive_root)
    assert len(entries) == 1
    entry = entries[0]
    assert entry["run_id"] == "20260101-000001-a"
    assert entry["receipt_file_sha256"] is None
    assert entry["receipt_sha256"] is None
    assert entry["receipt_schema_version"] is None
    assert entry["signature"] is None
    assert entry["status"] is None


def _write_verify_retention_config(tmp_path, **overrides):
    payload = {
        "version": 1,
        "depth": "repo",
        "harnesses": ["claude"],
        "owner": "claude",
        "includes": [],
    }
    payload.update(overrides)
    path = tmp_path / ".brigade" / "config.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload) + "\n")


def test_prune_verify_runs_respects_configured_keep_and_archive_dir(tmp_path):
    from brigade.work_cmd import helpers, verification

    root = helpers._verify_runs_root(tmp_path)
    root.mkdir(parents=True)
    names = ["20260101-000001-a", "20260101-000002-b", "20260101-000003-c"]
    for name in names:
        _write_verify_run_dir(root, name)
    _write_verify_retention_config(tmp_path, verify_runs_keep=1, verify_archive_dir="evidence/verify-archive")

    removed = verification._prune_verify_runs(tmp_path)

    assert removed == 2
    assert sorted(p.name for p in root.iterdir()) == ["20260101-000003-c"]
    archive_root = tmp_path / "evidence" / "verify-archive"
    assert (archive_root / "20260101-000001-a" / "receipt.json").is_file()
    assert (archive_root / "20260101-000002-b" / "receipt.json").is_file()
    assert len(_read_archive_index(archive_root)) == 2


def test_prune_verify_runs_defaults_archive_next_to_runs_root(tmp_path):
    from brigade.work_cmd import helpers, verification

    root = helpers._verify_runs_root(tmp_path)
    root.mkdir(parents=True)
    for name in ("20260101-000001-a", "20260101-000002-b"):
        _write_verify_run_dir(root, name)

    removed = verification._prune_verify_runs(tmp_path, keep=1)

    assert removed == 1
    default_archive = tmp_path / ".brigade" / "work" / "verify-archive"
    assert (default_archive / "20260101-000001-a" / "receipt.json").is_file()
    assert len(_read_archive_index(default_archive)) == 1


def test_prune_verify_runs_archive_disabled_matches_legacy_delete(tmp_path):
    from brigade.work_cmd import helpers, verification

    root = helpers._verify_runs_root(tmp_path)
    root.mkdir(parents=True)
    names = ["20260101-000001-a", "20260101-000002-b", "20260101-000003-c"]
    for name in names:
        _write_verify_run_dir(root, name)
    _write_verify_retention_config(tmp_path, verify_runs_keep=1, verify_archive_enabled=False)

    removed = verification._prune_verify_runs(tmp_path)

    assert removed == 2
    assert sorted(p.name for p in root.iterdir()) == ["20260101-000003-c"]
    assert not (tmp_path / ".brigade" / "work" / "verify-archive").exists()


def test_verify_run_finalization_archives_pruned_runs(tmp_path):
    from brigade.work_cmd import helpers

    _init_git_repo(tmp_path)
    root = helpers._verify_runs_root(tmp_path)
    root.mkdir(parents=True)
    # Seed the runs root at the retention cap so the new run triggers pruning.
    for index in range(50):
        _write_verify_run_dir(root, f"20260101-0000{index:02d}-seed")

    assert work_cmd.verify_run(target=tmp_path, commands=["python3 -c \"print('ok')\""]) == 0

    run_dirs = sorted(p.name for p in root.iterdir())
    assert len(run_dirs) == 50
    assert "20260101-000000-seed" not in run_dirs
    default_archive = tmp_path / ".brigade" / "work" / "verify-archive"
    assert (default_archive / "20260101-000000-seed" / "receipt.json").is_file()
    entries = _read_archive_index(default_archive)
    assert [entry["run_id"] for entry in entries] == ["20260101-000000-seed"]
    assert entries[0]["receipt_schema_version"] == 2


def test_outcome_health_flags_dormant_then_half_fed(tmp_path):
    from brigade import outcome_cmd

    dormant = outcome_cmd.health(tmp_path)
    assert dormant["record_count"] == 0 and dormant["verify_run_count"] == 0
    assert dormant["top_issue"]["name"] == "outcome_loop_dormant"

    _init_git_repo(tmp_path)
    assert work_cmd.verify_run(target=tmp_path, commands=["python3 -c \"print('ok')\""]) == 0
    half_fed = outcome_cmd.health(tmp_path)
    assert half_fed["verify_run_count"] >= 1 and half_fed["eligible_receipt_count"] == 0
    assert half_fed["top_issue"]["name"] == "outcome_loop_half_fed"
    assert "subject_binding" in half_fed["top_issue"]["detail"]


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


def _init_verify_target_with_head(target):
    target.mkdir(parents=True, exist_ok=True)
    _init_git_repo_with_head(target)


@pytest.mark.skipif(os.name != "posix", reason="requires POSIX shell env-prefix invocation")
def test_verify_reused_receipt_stamps_harness_session_from_outer_env_prefix(tmp_target, monkeypatch):
    """Regression #541: cache-hit receipts must stamp outer BRIGADE_CLAUDE_SESSION."""
    from brigade.claude_hooks.runtime import _session_fingerprint
    from brigade.work_cmd import verification

    _init_verify_target_with_head(tmp_target)
    graphtrail_bin = str(tmp_target / "missing-graphtrail")
    monkeypatch.setenv("GRAPHTRAIL_BIN", graphtrail_bin)

    fingerprint_a = _session_fingerprint("session-a-outer-prefix")
    fingerprint_b = _session_fingerprint("session-b-outer-prefix")
    verify_command = "true"
    target = str(tmp_target)
    brigade_cli = (
        f"{shlex.quote(sys.executable)} -m brigade work verify run "
        f"--target {shlex.quote(target)} --command {shlex.quote(verify_command)}"
    )
    subprocess_env = {**os.environ, "GRAPHTRAIL_BIN": graphtrail_bin}

    case_a = subprocess.run(
        ["/bin/sh", "-c", f"BRIGADE_CLAUDE_SESSION={fingerprint_a} {brigade_cli}"],
        cwd=tmp_target,
        env=subprocess_env,
        check=False,
        capture_output=True,
        text=True,
    )
    assert case_a.returncode == 0, case_a.stderr

    case_b = subprocess.run(
        [
            "/bin/sh",
            "-c",
            f"BRIGADE_CLAUDE_SESSION={fingerprint_b} PY=/fake/path {brigade_cli}",
        ],
        cwd=tmp_target,
        env=subprocess_env,
        check=False,
        capture_output=True,
        text=True,
    )
    assert case_b.returncode == 0, case_b.stderr

    receipts = verification._verify_receipts(tmp_target)
    assert len(receipts) == 2
    reused = receipts[0]
    fresh = receipts[1]
    assert reused["reused_from"] == fresh["run_id"]
    assert fresh["harness_session"] == {"harness": "claude", "fingerprint": fingerprint_a}
    assert reused["harness_session"] == {"harness": "claude", "fingerprint": fingerprint_b}
    assert reused["planned_commands"] == [verify_command]


def test_verify_reused_receipt_records_env_assignments_inside_command(tmp_target, monkeypatch):
    from brigade.work_cmd import verification

    _init_verify_target_with_head(tmp_target)
    monkeypatch.setenv("GRAPHTRAIL_BIN", str(tmp_target / "missing-graphtrail"))
    command = f'FOO=bar BAZ=qux {sys.executable} -c "print(1)"'

    assert verification.verify_run(target=tmp_target, commands=[command], timeout=60) == 0
    assert verification.verify_run(target=tmp_target, commands=[command], timeout=60) == 0

    receipts = verification._verify_receipts(tmp_target)
    assert len(receipts) == 2
    reused = receipts[0]
    fresh = receipts[1]
    assert reused["reused_from"] == fresh["run_id"]
    assert reused["commands"][0]["env"] == ["BAZ", "FOO"]
    assert reused["planned_commands"] == [command]


def test_verify_reuses_identical_tree(tmp_target, monkeypatch):
    from brigade.work_cmd import verification

    _init_verify_target_with_head(tmp_target)
    monkeypatch.setenv("GRAPHTRAIL_BIN", str(tmp_target / "missing-graphtrail"))

    rc1 = verification.verify_run(target=tmp_target, commands=["true"], timeout=60)
    assert rc1 == 0
    rc2 = verification.verify_run(target=tmp_target, commands=["true"], timeout=60)
    assert rc2 == 0
    receipts = verification._verify_receipts(tmp_target)
    assert len(receipts) == 2
    newest = receipts[0]
    assert newest["status"] == "completed"
    assert newest["reused_from"] == receipts[1]["run_id"]
    # the reused receipt carries forward the prior run's command records
    assert newest["commands"] == receipts[1]["commands"]


def test_verify_no_reuse_flag_forces_run(tmp_target, monkeypatch):
    from brigade.work_cmd import verification

    _init_verify_target_with_head(tmp_target)
    monkeypatch.setenv("GRAPHTRAIL_BIN", str(tmp_target / "missing-graphtrail"))

    verification.verify_run(target=tmp_target, commands=["true"], timeout=60)
    verification.verify_run(target=tmp_target, commands=["true"], timeout=60, reuse=False)
    receipts = verification._verify_receipts(tmp_target)
    assert "reused_from" not in receipts[0]


def test_verify_dirty_tree_not_reused(tmp_target, monkeypatch):
    from brigade.work_cmd import verification

    _init_verify_target_with_head(tmp_target)
    monkeypatch.setenv("GRAPHTRAIL_BIN", str(tmp_target / "missing-graphtrail"))

    verification.verify_run(target=tmp_target, commands=["true"], timeout=60)
    (tmp_target / "newfile.txt").write_text("x\n")
    verification.verify_run(target=tmp_target, commands=["true"], timeout=60)
    receipts = verification._verify_receipts(tmp_target)
    assert "reused_from" not in receipts[0]


def test_verify_failed_receipt_not_reused(tmp_target, monkeypatch):
    from brigade.work_cmd import verification

    _init_verify_target_with_head(tmp_target)
    monkeypatch.setenv("GRAPHTRAIL_BIN", str(tmp_target / "missing-graphtrail"))

    verification.verify_run(target=tmp_target, commands=["false"], timeout=60)
    rc = verification.verify_run(target=tmp_target, commands=["false"], timeout=60)
    assert rc != 0
    receipts = verification._verify_receipts(tmp_target)
    assert "reused_from" not in receipts[0]


def test_verify_warns_before_retrying_uncaptured_failed_command(tmp_target, monkeypatch, capsys):
    from brigade.work_cmd import verification

    _init_verify_target_with_head(tmp_target)
    monkeypatch.setenv("GRAPHTRAIL_BIN", str(tmp_target / "missing-graphtrail"))

    verification.verify_run(target=tmp_target, commands=["false"], timeout=60)
    failed = verification._verify_receipts(tmp_target)[0]
    rc = verification.verify_run(target=tmp_target, commands=["false"], timeout=60)
    assert rc != 0
    err = capsys.readouterr().err
    assert f"warning: brigade outcome capture brigade-work --run-id {failed['run_id']}" in err


def test_verify_blocks_before_retrying_uncaptured_failed_command(tmp_target, monkeypatch, capsys):
    from brigade.work_cmd import verification

    _init_verify_target_with_head(tmp_target)
    _write_brigade_config(tmp_target, capture_before_retry="block")
    monkeypatch.setenv("GRAPHTRAIL_BIN", str(tmp_target / "missing-graphtrail"))

    verification.verify_run(target=tmp_target, commands=["false"], timeout=60)
    failed = verification._verify_receipts(tmp_target)[0]
    before = _count_verify_run_dirs(tmp_target)
    rc = verification.verify_run(target=tmp_target, commands=["false"], timeout=60)
    assert rc == 1
    assert _count_verify_run_dirs(tmp_target) == before
    err = capsys.readouterr().err
    assert f"error: brigade outcome capture brigade-work --run-id {failed['run_id']}" in err


def test_verify_off_allows_silent_retry_of_uncaptured_failed_command(tmp_target, monkeypatch, capsys):
    from brigade.work_cmd import verification

    _init_verify_target_with_head(tmp_target)
    _write_brigade_config(tmp_target, capture_before_retry="off")
    monkeypatch.setenv("GRAPHTRAIL_BIN", str(tmp_target / "missing-graphtrail"))

    verification.verify_run(target=tmp_target, commands=["false"], timeout=60)
    rc = verification.verify_run(target=tmp_target, commands=["false"], timeout=60)
    assert rc != 0
    err = capsys.readouterr().err
    assert "brigade outcome capture" not in err


def test_verify_captured_failed_command_retries_silently(tmp_target, monkeypatch, capsys):
    from brigade import outcome_cmd
    from brigade.work_cmd import verification

    _init_verify_target_with_head(tmp_target)
    monkeypatch.setenv("GRAPHTRAIL_BIN", str(tmp_target / "missing-graphtrail"))

    verification.verify_run(target=tmp_target, commands=["false"], timeout=60)
    failed = verification._verify_receipts(tmp_target)[0]
    assert outcome_cmd.capture(target=tmp_target, artifact_id="brigade-work", run_id=failed["run_id"]) == 0
    capsys.readouterr()
    rc = verification.verify_run(target=tmp_target, commands=["false"], timeout=60)
    assert rc != 0
    err = capsys.readouterr().err
    assert "brigade outcome capture" not in err


def test_verify_first_run_and_passed_retry_stay_silent(tmp_target, monkeypatch, capsys):
    from brigade.work_cmd import verification

    _init_verify_target_with_head(tmp_target)
    monkeypatch.setenv("GRAPHTRAIL_BIN", str(tmp_target / "missing-graphtrail"))

    rc = verification.verify_run(target=tmp_target, commands=["true"], timeout=60)
    assert rc == 0
    err = capsys.readouterr().err
    assert "brigade outcome capture" not in err

    rc = verification.verify_run(target=tmp_target, commands=["true"], timeout=60, reuse=False)
    assert rc == 0
    err = capsys.readouterr().err
    assert "brigade outcome capture" not in err


def test_verify_pass_after_failure_makes_next_retry_silent(tmp_target, monkeypatch, capsys):
    from brigade.work_cmd import verification

    _init_verify_target_with_head(tmp_target)
    monkeypatch.setenv("GRAPHTRAIL_BIN", str(tmp_target / "missing-graphtrail"))
    (tmp_target / "check.py").write_text(
        "from pathlib import Path\n"
        "marker = Path('passed-once')\n"
        "if not marker.exists():\n"
        "    marker.write_text('ready')\n"
        "    raise SystemExit(1)\n"
    )

    command = "python3 check.py"
    assert verification.verify_run(target=tmp_target, commands=[command], timeout=60) != 0
    assert verification.verify_run(target=tmp_target, commands=[command], timeout=60) == 0
    capsys.readouterr()

    assert verification.verify_run(target=tmp_target, commands=[command], timeout=60, reuse=False) == 0
    assert "brigade outcome capture" not in capsys.readouterr().err


def test_verify_capture_before_retry_matches_exact_command_identity(tmp_target, monkeypatch, capsys):
    from brigade.work_cmd import verification

    _init_verify_target_with_head(tmp_target)
    monkeypatch.setenv("GRAPHTRAIL_BIN", str(tmp_target / "missing-graphtrail"))

    verification.verify_run(target=tmp_target, commands=["VAR=1 false"], timeout=60)
    failed = verification._verify_receipts(tmp_target)[0]
    rc = verification.verify_run(target=tmp_target, commands=["false"], timeout=60)
    assert rc != 0
    err = capsys.readouterr().err
    assert "brigade outcome capture" not in err

    rc = verification.verify_run(target=tmp_target, commands=["VAR=1 false"], timeout=60)
    assert rc != 0
    err = capsys.readouterr().err
    assert f"warning: brigade outcome capture brigade-work --run-id {failed['run_id']}" in err


def test_verify_command_identity_normalizes_whitespace(tmp_target, monkeypatch, capsys):
    from brigade.work_cmd import verification

    _init_verify_target_with_head(tmp_target)
    monkeypatch.setenv("GRAPHTRAIL_BIN", str(tmp_target / "missing-graphtrail"))

    verification.verify_run(target=tmp_target, commands=["VAR=1  false"], timeout=60)
    failed = verification._verify_receipts(tmp_target)[0]
    verification.verify_run(target=tmp_target, commands=["VAR=1 false"], timeout=60)
    err = capsys.readouterr().err
    assert f"warning: brigade outcome capture brigade-work --run-id {failed['run_id']}" in err


def _init_git_repo_with_fixed_head(path):
    """Init a git repo whose HEAD commit hash is deterministic across calls.

    Pinning author/committer dates makes two repos produce an identical HEAD
    so a tree-fingerprint regression test can isolate the untracked-segment
    boundary collision (``{"a": "bc"}`` vs ``{"ab": "c"}``) instead of being
    masked by differing commit hashes.
    """
    subprocess.run(["git", "init"], cwd=path, check=True, stdout=subprocess.DEVNULL)
    subprocess.run(["git", "config", "user.name", "Test User"], cwd=path, check=True)
    subprocess.run(["git", "config", "user.email", "test@example.invalid"], cwd=path, check=True)
    (path / ".gitignore").write_text(".brigade/\n")
    env = {
        **os.environ,
        "GIT_AUTHOR_DATE": "2026-01-01T00:00:00Z",
        "GIT_COMMITTER_DATE": "2026-01-01T00:00:00Z",
    }
    subprocess.run(["git", "add", ".gitignore"], cwd=path, check=True, stdout=subprocess.DEVNULL)
    subprocess.run(["git", "commit", "-m", "init"], cwd=path, check=True, stdout=subprocess.DEVNULL, env=env)


def test_tree_fingerprint_distinguishes_untracked_name_content_boundary(tmp_path):
    # Two repos with identical committed HEAD content but untracked sets that
    # concatenate to the same byte stream ({"a": "bc"} vs {"ab": "c"}) must
    # fingerprint to distinct non-None values: the name/content boundary is
    # part of the hashed identity, not just the raw concatenation.
    from brigade.work_cmd import verification

    target_a = tmp_path / "repo-a"
    target_b = tmp_path / "repo-b"
    target_a.mkdir()
    target_b.mkdir()
    _init_git_repo_with_fixed_head(target_a)
    _init_git_repo_with_fixed_head(target_b)

    head_a = subprocess.check_output(["git", "-C", str(target_a), "rev-parse", "HEAD"], text=True).strip()
    head_b = subprocess.check_output(["git", "-C", str(target_b), "rev-parse", "HEAD"], text=True).strip()
    assert head_a == head_b, "test setup: HEAD commits must be identical to isolate the untracked collision"

    (target_a / "a").write_text("bc")
    (target_b / "ab").write_text("c")

    fp_a = verification._tree_fingerprint(target_a)
    fp_b = verification._tree_fingerprint(target_b)

    assert fp_a is not None
    assert fp_b is not None
    assert fp_a != fp_b


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


@pytest.mark.parametrize(
    ("option", "value"),
    [
        ("--command", "./scripts/verify"),
        ("--argv-json", json.dumps(["./scripts/verify"])),
    ],
)
def test_work_verify_run_resolves_relative_executable_from_target(tmp_path, monkeypatch, capsys, option, value):
    target = tmp_path / "repo"
    caller = tmp_path / "caller"
    target.mkdir()
    caller.mkdir()
    _init_git_repo(target)
    script = target / "scripts" / "verify"
    script.parent.mkdir()
    script.write_text("#!/bin/sh\nprintf 'target-ok\\n'\n")
    script.chmod(0o755)
    monkeypatch.chdir(caller)

    rc = cli.main(
        [
            "work",
            "verify",
            "run",
            "--target",
            str(target),
            option,
            value,
            "--json",
        ]
    )

    receipt = json.loads(capsys.readouterr().out)
    assert rc == 0
    assert receipt["status"] == "completed"
    assert receipt["commands"][0]["argv"] == ["./scripts/verify"]
    assert receipt["commands"][0]["stdout_summary"] == "target-ok"


def test_work_verify_execution_argv_uses_resolved_target_path(tmp_path):
    from brigade.work_cmd import verification

    target = tmp_path / "repo"
    script = target / "scripts" / "verify"
    script.parent.mkdir(parents=True)
    script.write_text("#!/bin/sh\n")
    relative_argv = ["./scripts/verify", "--quick"]

    assert verification._verify_execution_argv(relative_argv, target) == [str(script), "--quick"]
    assert relative_argv == ["./scripts/verify", "--quick"]
    assert verification._verify_execution_argv([str(script), "--quick"], target) == [str(script), "--quick"]
    assert verification._verify_execution_argv(["python3", "-V"], target) == ["python3", "-V"]


def test_work_verify_run_records_target_relative_process_start_failure(tmp_path, monkeypatch, capsys):
    target = tmp_path / "repo"
    caller = tmp_path / "caller"
    target.mkdir()
    caller.mkdir()
    _init_git_repo(target)
    script = target / "scripts" / "verify"
    script.parent.mkdir()
    script.write_text("#!/bin/sh\n")
    monkeypatch.chdir(caller)

    rc = cli.main(
        [
            "work",
            "verify",
            "run",
            "--target",
            str(target),
            "--command",
            "./scripts/verify",
            "--json",
        ]
    )

    receipt = json.loads(capsys.readouterr().out)
    command = receipt["commands"][0]
    assert rc == 127
    assert receipt["status"] == "failed"
    assert command["status"] == "failed"
    assert command["exit_code"] == 127
    assert command["stderr_summary"]
    assert Path(command["stderr_log_path"]).is_file()


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
            'python3 -c "print(1); print(2)"',
            "--json",
        ]
    )
    receipt = json.loads(capsys.readouterr().out)
    assert rc != 0
    assert receipt["status"] == "rejected"
    assert receipt["commands"][0]["status"] == "rejected"
    assert "shell metacharacters" in receipt["commands"][0]["stderr_summary"]
    assert "--argv-json" in receipt["commands"][0]["stderr_summary"]


@pytest.mark.parametrize(
    ("option", "value"),
    [
        ("--command", "bash ./check.sh"),
        ("--argv-json", json.dumps(["bash", "-c", "true"])),
    ],
)
def test_work_verify_run_rejects_shell_interpreter_with_remedy(tmp_path, capsys, option, value):
    # A shell interpreter is never a valid verify executable (verify runs argv
    # directly with shell=False). Both the --command and --argv-json paths reject
    # it, and the message must name the remedy - mirroring the metacharacter
    # branch - so a caller is not left at a dead end. It must NOT point at
    # --argv-json, which applies the same block.
    _init_git_repo(tmp_path)

    rc = cli.main(["work", "verify", "run", "--target", str(tmp_path), option, value, "--json"])
    receipt = json.loads(capsys.readouterr().out)
    summary = receipt["commands"][0]["stderr_summary"]
    assert rc != 0
    assert receipt["status"] == "rejected"
    assert receipt["commands"][0]["status"] == "rejected"
    assert "high-risk verification command: bash" in summary
    assert "resolvable executable" in summary
    assert "--argv-json" not in summary


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


def test_work_verify_receipt_compacts_prior_nested_evidence(tmp_path, capsys, monkeypatch):
    prior_dir = tmp_path / ".brigade" / "work" / "verify-runs" / "20260708-120000-work-verify-prior"
    prior_digest = "a" * 64
    prior_dir.mkdir(parents=True)
    _write_json(
        prior_dir / "receipt.json",
        {
            "run_id": prior_dir.name,
            "status": "completed",
            "path": str(prior_dir),
            "started_at": "2026-07-08T12:00:00+00:00",
            "digests": {"receipt_sha256": prior_digest},
            "evidence": {
                "latest_verify": {
                    "run_id": "20260707-120000-work-verify-older",
                    "evidence": {"latest_verify": {"run_id": "nested"}},
                }
            },
        },
    )
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

    assert receipt["evidence"]["latest_verify"] == {
        "run_id": prior_dir.name,
        "status": "completed",
        "path": str(prior_dir),
        "digest": prior_digest,
    }
    assert "evidence" not in receipt["evidence"]["latest_verify"]


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


def _write_brigade_config(
    tmp_path,
    *,
    graphtrail_delta_timeout_seconds: float | None = None,
    capture_before_retry: str | None = None,
) -> None:
    payload = {
        "version": 1,
        "depth": "repo",
        "harnesses": ["codex"],
        "owner": "this-repo",
        "includes": [],
    }
    if graphtrail_delta_timeout_seconds is not None:
        payload["graphtrail_delta_timeout_seconds"] = graphtrail_delta_timeout_seconds
    if capture_before_retry is not None:
        payload["capture_before_retry"] = capture_before_retry
    brigade = tmp_path / ".brigade"
    brigade.mkdir(parents=True, exist_ok=True)
    (brigade / "config.json").write_text(json.dumps(payload, indent=2) + "\n")


def _seed_graphtrail_db(tmp_path: Path) -> Path:
    db_dir = tmp_path / ".graphtrail"
    db_dir.mkdir(parents=True, exist_ok=True)
    db_path = db_dir / "graphtrail.db"
    with sqlite3.connect(db_path) as con:
        con.execute("create table if not exists symbols (name text)")
        con.execute("insert into symbols values ('stale-baseline')")
    return db_path


def _count_verify_run_dirs(target: Path) -> int:
    from brigade.work_cmd import helpers

    root = helpers._verify_runs_root(target)
    if not root.is_dir():
        return 0
    return sum(1 for entry in root.iterdir() if entry.is_dir())


# Tight-timeout tests must leave headroom for subprocess startup on CI while keeping
# deliberate stage delays well above the configured graphtrail timeout.
_GRAPHTRAIL_TIGHT_TIMEOUT_SECONDS = 0.25
_GRAPHTRAIL_SLOW_STAGE_DELAY_SECONDS = 0.6


def _derace_graphtrail_sync_timing(monkeypatch, graphtrail_bin: Path, *, time_out_sync_call: int | None = None) -> None:
    """Make fake-graphtrail sync timing deterministic instead of racing wall clock.

    A sync call the test needs to succeed must never lose a wall-clock race
    against the tight subprocess timeout on a loaded CI runner (interpreter
    startup alone can exceed it), so sync invocations of the fake binary run
    with no timeout. The ``time_out_sync_call``-th sync instead raises
    ``TimeoutExpired`` without starting a process, driving the real timeout
    handling in ``graphtrail_delta._run_graphtrail`` deterministically.
    """
    real_run = subprocess.run
    sync_calls = {"count": 0}

    def patched_run(argv, *args, **kwargs):
        if isinstance(argv, list) and argv and argv[0] == str(graphtrail_bin) and "sync" in argv:
            sync_calls["count"] += 1
            if time_out_sync_call is not None and sync_calls["count"] >= time_out_sync_call:
                raise subprocess.TimeoutExpired(cmd=argv, timeout=kwargs.get("timeout") or 0.0)
            kwargs["timeout"] = None
        return real_run(argv, *args, **kwargs)

    monkeypatch.setattr(subprocess, "run", patched_run)


def _write_fake_graphtrail(
    tmp_path,
    *,
    mode: str = "ok",
    sync_delay_seconds: float = 0.0,
    diff_delay_seconds: float = 0.0,
    create_db_before_delay: bool = False,
) -> Path:
    script = tmp_path / "fake-graphtrail.py"
    script.write_text(
        """
import hashlib
import json
import os
import sqlite3
import sys
import time
from pathlib import Path

args = sys.argv[1:]
db = Path(args[args.index("--db") + 1]) if "--db" in args else Path(".graphtrail/graphtrail.db")
command = args[args.index(str(db)) + 1] if str(db) in args and args.index(str(db)) + 1 < len(args) else ""
mode = os.environ.get("FAKE_GRAPHTRAIL_MODE", "ok")
sync_delay_seconds = float(os.environ.get("FAKE_GRAPHTRAIL_SYNC_SECONDS", "0"))
diff_delay_seconds = float(os.environ.get("FAKE_GRAPHTRAIL_DIFF_SECONDS", "0"))
create_db_before_delay = os.environ.get("FAKE_GRAPHTRAIL_CREATE_DB_BEFORE_DELAY", "") == "1"

# Mirror the real clap CLI strictly: `sync` rejects --json, `diff` requires
# --before/--after/--json. JSON shape follows graphtrail's diff golden fixture.
if command == "sync":
    if "--json" in args:
        print("error: unexpected argument '--json' found", file=sys.stderr)
        raise SystemExit(2)
    if create_db_before_delay:
        db.parent.mkdir(parents=True, exist_ok=True)
        with sqlite3.connect(db) as con:
            con.execute("create table if not exists symbols (name text)")
    if sync_delay_seconds > 0:
        time.sleep(sync_delay_seconds)
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
    if diff_delay_seconds > 0:
        time.sleep(diff_delay_seconds)
    if mode == "malformed-diff":
        print("{not-json")
        raise SystemExit(0)

    def node(name, line=1):
        return {
            "kind": "function",
            "qualified_name": name,
            "file_path": "pkg/mod.py",
            "start_line": line,
            "end_line": line + 1,
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
        "#!/bin/sh\n"
        f"FAKE_GRAPHTRAIL_MODE={mode}\n"
        f"FAKE_GRAPHTRAIL_SYNC_SECONDS={sync_delay_seconds}\n"
        f"FAKE_GRAPHTRAIL_DIFF_SECONDS={diff_delay_seconds}\n"
        f"FAKE_GRAPHTRAIL_CREATE_DB_BEFORE_DELAY={'1' if create_db_before_delay else '0'}\n"
        "export FAKE_GRAPHTRAIL_MODE FAKE_GRAPHTRAIL_SYNC_SECONDS FAKE_GRAPHTRAIL_DIFF_SECONDS "
        "FAKE_GRAPHTRAIL_CREATE_DB_BEFORE_DELAY\n"
        "export PYTHONDONTWRITEBYTECODE=1\n"
        f'exec {os.environ.get("PYTHON", "python3")} -S {script} "$@"\n'
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


def test_work_verify_compact_delta_emits_exportable_code_references(tmp_path, capsys, monkeypatch):
    """Exercise capture_after_and_diff, compaction, receipt storage, and export together."""
    _init_git_repo_with_head(tmp_path)
    subprocess.run(
        ["git", "remote", "add", "origin", "https://github.com/escoffier-labs/brigade.git"],
        cwd=tmp_path,
        check=True,
        stdout=subprocess.DEVNULL,
    )
    monkeypatch.setenv("GRAPHTRAIL_BIN", str(_write_fake_graphtrail(tmp_path)))

    assert work_cmd.verify_run(target=tmp_path, commands=["python3 -c \"print('ok')\""], json_output=True) == 0
    receipt = json.loads(capsys.readouterr().out)
    delta = receipt["code_graph_delta"]
    assert len(delta["code_reference_nodes"]) == 20
    assert delta["code_reference_nodes"][0] == {
        "change_kind": "added",
        "file_path": "pkg/mod.py",
        "kind": "function",
        "qualified_name": "pkg.new_a",
        "start_line": 1,
        "end_line": 2,
    }

    assert receipts_cmd.export_miseledger(target=tmp_path) == 0
    row = json.loads(capsys.readouterr().out)
    references = row["item"]["metadata"]["code_references"]
    assert references[0]["repository"] == "escoffier-labs/brigade"
    assert references[0]["source_span"] == {"start_line": 1, "line_count": 2}
    assert row["item"]["metadata"]["code_references_total"] == 28
    assert row["item"]["metadata"]["code_references_truncated"] is True


def test_code_reference_compaction_sorts_valid_nodes_and_counts_malformed_candidates_out():
    compact = graphtrail_delta._compact(
        {
            "status": "ok",
            "ok": True,
            "added_nodes": [
                {"kind": "function", "qualified_name": "zeta", "file_path": "pkg/z.py", "start_line": 4, "end_line": 4},
                {
                    "kind": "function",
                    "qualified_name": "alpha",
                    "file_path": "pkg/a.py",
                    "start_line": 2,
                    "end_line": 3,
                },
                {"kind": "function", "qualified_name": "empty_path", "file_path": "", "start_line": 4, "end_line": 4},
                {
                    "kind": "function",
                    "qualified_name": "absolute",
                    "file_path": "/pkg/a.py",
                    "start_line": 4,
                    "end_line": 4,
                },
                {
                    "kind": "function",
                    "qualified_name": "traversal",
                    "file_path": "pkg/../a.py",
                    "start_line": 4,
                    "end_line": 4,
                },
                {
                    "kind": "unsupported",
                    "qualified_name": "unknown_kind",
                    "file_path": "pkg/a.py",
                    "start_line": 4,
                    "end_line": 4,
                },
                {"kind": "function", "qualified_name": "", "file_path": "pkg/a.py", "start_line": 4, "end_line": 4},
                {"kind": "", "qualified_name": "empty_kind", "file_path": "pkg/a.py", "start_line": 4, "end_line": 4},
                {
                    "kind": "function",
                    "qualified_name": "reversed",
                    "file_path": "pkg/a.py",
                    "start_line": 7,
                    "end_line": 6,
                },
                {"kind": "function", "qualified_name": "missing", "file_path": "pkg/a.py", "start_line": 8},
                "not-a-node",
            ],
        }
    )

    assert compact["code_reference_nodes"] == [
        {
            "change_kind": "added",
            "file_path": "pkg/a.py",
            "kind": "function",
            "qualified_name": "alpha",
            "start_line": 2,
            "end_line": 3,
        },
        {
            "change_kind": "added",
            "file_path": "pkg/z.py",
            "kind": "function",
            "qualified_name": "zeta",
            "start_line": 4,
            "end_line": 4,
        },
    ]
    assert compact["code_reference_nodes_total"] == 2
    assert compact["code_reference_nodes_truncated"] is False


def test_code_reference_compaction_counts_only_valid_candidates_before_the_cap():
    valid_nodes = [
        {
            "kind": "function",
            "qualified_name": f"pkg.symbol_{number:02d}",
            "file_path": "pkg/mod.py",
            "start_line": number,
            "end_line": number,
        }
        for number in range(1, 22)
    ]

    compact = graphtrail_delta._compact({"status": "ok", "ok": True, "added_nodes": list(reversed(valid_nodes))})

    assert len(compact["code_reference_nodes"]) == 20
    assert compact["code_reference_nodes_total"] == 21
    assert compact["code_reference_nodes_truncated"] is True
    assert (
        compact["code_reference_nodes"]
        == sorted(
            [{"change_kind": "added", **node} for node in valid_nodes],
            key=lambda node: json.dumps(node, sort_keys=True, separators=(",", ":")),
        )[:20]
    )


@pytest.mark.parametrize(
    ("retained_count", "declared_total", "declared_truncated", "malformed", "expected_total", "expected_truncated"),
    [
        (19, 19, False, False, 19, False),
        (19, 20, False, False, 19, False),
        (19, 20, True, False, 19, False),
        (20, 20, True, False, 20, False),
        (20, 21, False, False, 20, False),
        (20, 28, True, False, 28, True),
        (20, 28, True, True, 20, False),
    ],
)
def test_code_reference_compaction_trusts_only_exact_declared_candidate_metadata(
    retained_count, declared_total, declared_truncated, malformed, expected_total, expected_truncated
):
    nodes = [
        {
            "change_kind": "added",
            "kind": "function",
            "qualified_name": f"pkg.symbol_{number:02d}",
            "file_path": "pkg/mod.py",
            "start_line": number,
            "end_line": number,
        }
        for number in range(1, retained_count + 1)
    ]
    if malformed:
        nodes.append({"change_kind": "added", "kind": "function", "qualified_name": "", "file_path": "pkg/mod.py"})

    compact = graphtrail_delta._compact(
        {
            "status": "ok",
            "ok": True,
            "code_reference_nodes": nodes,
            "code_reference_nodes_total": declared_total,
            "code_reference_nodes_truncated": declared_truncated,
        }
    )

    assert compact["code_reference_nodes_total"] == expected_total
    assert compact["code_reference_nodes_truncated"] is expected_truncated


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


def test_work_verify_graphtrail_delta_config_timeout_reaches_pre_and_post_sync(tmp_path, capsys, monkeypatch):
    _init_git_repo(tmp_path)
    _write_brigade_config(tmp_path, graphtrail_delta_timeout_seconds=25)
    graphtrail = _write_fake_graphtrail(tmp_path)
    monkeypatch.setenv("GRAPHTRAIL_BIN", str(graphtrail))
    recorded: list[tuple[str, float | None]] = []
    real_capture_before = graphtrail_delta.capture_before
    real_capture_after = graphtrail_delta.capture_after_and_diff

    def spy_before(target, run_dir, **kwargs):
        recorded.append(("before", kwargs.get("timeout")))
        return real_capture_before(target, run_dir, **kwargs)

    def spy_after(target, run_dir, before, **kwargs):
        recorded.append(("after", kwargs.get("timeout")))
        return real_capture_after(target, run_dir, before, **kwargs)

    monkeypatch.setattr(graphtrail_delta, "capture_before", spy_before)
    monkeypatch.setattr(graphtrail_delta, "capture_after_and_diff", spy_after)

    assert work_cmd.verify_run(target=tmp_path, commands=["python3 -c \"print('ok')\""], json_output=True) == 0
    capsys.readouterr()

    assert recorded == [("before", 25.0), ("after", 25.0)]


def test_work_verify_graphtrail_delta_cli_override_precedes_config(tmp_path, capsys, monkeypatch):
    _init_git_repo(tmp_path)
    _write_brigade_config(tmp_path, graphtrail_delta_timeout_seconds=25)
    graphtrail = _write_fake_graphtrail(tmp_path)
    monkeypatch.setenv("GRAPHTRAIL_BIN", str(graphtrail))
    recorded: list[tuple[str, float | None]] = []
    real_capture_before = graphtrail_delta.capture_before
    real_capture_after = graphtrail_delta.capture_after_and_diff

    def spy_before(target, run_dir, **kwargs):
        recorded.append(("before", kwargs.get("timeout")))
        return real_capture_before(target, run_dir, **kwargs)

    def spy_after(target, run_dir, before, **kwargs):
        recorded.append(("after", kwargs.get("timeout")))
        return real_capture_after(target, run_dir, before, **kwargs)

    monkeypatch.setattr(graphtrail_delta, "capture_before", spy_before)
    monkeypatch.setattr(graphtrail_delta, "capture_after_and_diff", spy_after)

    assert (
        work_cmd.verify_run(
            target=tmp_path,
            commands=["python3 -c \"print('ok')\""],
            graphtrail_timeout=45,
            json_output=True,
        )
        == 0
    )
    capsys.readouterr()

    assert recorded == [("before", 45.0), ("after", 45.0)]


def test_work_verify_graphtrail_delta_slow_sync_succeeds_with_configured_timeout(tmp_path, capsys, monkeypatch):
    _init_git_repo(tmp_path)
    _write_brigade_config(tmp_path, graphtrail_delta_timeout_seconds=1.0)
    graphtrail = _write_fake_graphtrail(tmp_path, sync_delay_seconds=0.2)
    monkeypatch.setenv("GRAPHTRAIL_BIN", str(graphtrail))

    assert work_cmd.verify_run(target=tmp_path, commands=["python3 -c \"print('ok')\""], json_output=True) == 0
    receipt = json.loads(capsys.readouterr().out)
    sidecar = json.loads((Path(receipt["path"]) / "graph-delta.json").read_text())

    assert receipt["code_graph_delta"]["status"] == "ok"
    assert sidecar["graphtrail_timeout_seconds"] == 1.0
    assert sidecar["commands"]["before_sync"]["timed_out"] is False
    assert sidecar["commands"]["after_sync"]["timed_out"] is False


def test_work_verify_graphtrail_delta_timeout_evidence_and_verify_exit_unchanged(tmp_path, capsys, monkeypatch):
    _init_git_repo(tmp_path)
    graphtrail = _write_fake_graphtrail(tmp_path, sync_delay_seconds=0.2)
    monkeypatch.setenv("GRAPHTRAIL_BIN", str(graphtrail))

    assert (
        work_cmd.verify_run(
            target=tmp_path,
            commands=["python3 -c \"print('ok')\""],
            graphtrail_timeout=0.05,
            json_output=True,
        )
        == 0
    )
    receipt = json.loads(capsys.readouterr().out)
    sidecar = json.loads((Path(receipt["path"]) / "graph-delta.json").read_text())
    before_sync = sidecar["commands"]["before_sync"]
    compact_delta = receipt["code_graph_delta"]

    assert receipt["commands"][0]["status"] == "completed"
    assert receipt["commands"][0]["exit_code"] == 0
    assert compact_delta["status"] == "sync_timed_out"
    assert sidecar["status"] == "sync_timed_out"
    assert sidecar["graphtrail_timeout_seconds"] == 0.05
    assert compact_delta["graphtrail_timeout_seconds"] == 0.05
    assert before_sync["timed_out"] is True
    assert before_sync["returncode"] == 124
    assert before_sync["duration_seconds"] >= 0.05
    assert before_sync["stderr"]
    compact_before_sync = compact_delta["commands"]["before_sync"]
    assert compact_before_sync["timed_out"] is True
    assert compact_before_sync["duration_seconds"] >= 0.05
    assert compact_before_sync["stderr"]
    assert before_sync["stage"] == "initial-index"


def test_work_verify_graphtrail_delta_cold_initial_early_db_timeout_stays_sync_timed_out(tmp_path, capsys, monkeypatch):
    _init_git_repo(tmp_path)
    graphtrail = _write_fake_graphtrail(
        tmp_path,
        sync_delay_seconds=0.2,
        create_db_before_delay=True,
    )
    monkeypatch.setenv("GRAPHTRAIL_BIN", str(graphtrail))

    assert (
        work_cmd.verify_run(
            target=tmp_path,
            commands=["python3 -c \"print('ok')\""],
            graphtrail_timeout=0.05,
            json_output=True,
        )
        == 0
    )
    receipt = json.loads(capsys.readouterr().out)
    sidecar = json.loads((Path(receipt["path"]) / "graph-delta.json").read_text())
    before_sync = sidecar["commands"]["before_sync"]
    compact_delta = receipt["code_graph_delta"]

    assert receipt["commands"][0]["exit_code"] == 0
    assert compact_delta["status"] == "sync_timed_out"
    assert sidecar["status"] == "sync_timed_out"
    assert "stale_graph_used" not in compact_delta
    assert "stale_graph_used" not in sidecar
    assert before_sync["stage"] == "initial-index"
    assert before_sync["timed_out"] is True
    assert sidecar["commands"]["after_sync"] is None


def test_work_verify_graphtrail_delta_invalid_config_timeout_returns_2_without_orphan_run(tmp_path, capsys):
    _init_git_repo(tmp_path)
    _write_brigade_config(tmp_path, graphtrail_delta_timeout_seconds=0)
    assert _count_verify_run_dirs(tmp_path) == 0

    rc = work_cmd.verify_run(
        target=tmp_path,
        commands=["python3 -c \"print('ok')\""],
        json_output=True,
    )
    err = capsys.readouterr().err

    assert rc == 2
    assert "error:" in err
    assert "graphtrail_delta_timeout_seconds must be a positive number" in err
    assert "Traceback" not in err
    assert _count_verify_run_dirs(tmp_path) == 0


def test_work_verify_graphtrail_delta_post_sync_timeout_after_successful_pre_sync(tmp_path, capsys, monkeypatch):
    _init_git_repo(tmp_path)
    graphtrail = _write_fake_graphtrail(tmp_path)
    monkeypatch.setenv("GRAPHTRAIL_BIN", str(graphtrail))
    _derace_graphtrail_sync_timing(monkeypatch, graphtrail, time_out_sync_call=2)

    assert (
        work_cmd.verify_run(
            target=tmp_path,
            commands=["python3 -c \"print('ok')\""],
            graphtrail_timeout=_GRAPHTRAIL_TIGHT_TIMEOUT_SECONDS,
            json_output=True,
        )
        == 0
    )
    receipt = json.loads(capsys.readouterr().out)
    sidecar = json.loads((Path(receipt["path"]) / "graph-delta.json").read_text())
    before_sync = sidecar["commands"]["before_sync"]
    after_sync = sidecar["commands"]["after_sync"]

    assert receipt["commands"][0]["status"] == "completed"
    assert receipt["commands"][0]["exit_code"] == 0
    assert receipt["code_graph_delta"]["status"] == "sync_timed_out"
    assert sidecar["status"] == "sync_timed_out"
    assert before_sync["timed_out"] is False
    assert after_sync["stage"] == "incremental-sync"
    assert after_sync["timed_out"] is True


@pytest.mark.parametrize(
    "bad_timeout",
    [0, -1, True, float("nan"), float("inf")],
    ids=["zero", "negative", "boolean", "nan", "infinity"],
)
def test_work_verify_graphtrail_delta_rejects_invalid_per_invocation_timeout_override(tmp_path, capsys, bad_timeout):
    _init_git_repo(tmp_path)

    rc = work_cmd.verify_run(
        target=tmp_path,
        commands=["python3 -c \"print('ok')\""],
        graphtrail_timeout=bad_timeout,
    )
    err = capsys.readouterr().err

    assert rc == 2
    assert "error:" in err
    assert "--graphtrail-timeout must be a positive number" in err
    assert "Traceback" not in err
    assert _count_verify_run_dirs(tmp_path) == 0


def test_work_verify_graphtrail_delta_preexisting_db_timeout_uses_stale_baseline(tmp_path, capsys, monkeypatch):
    _init_git_repo(tmp_path)
    _seed_graphtrail_db(tmp_path)
    graphtrail = _write_fake_graphtrail(tmp_path, sync_delay_seconds=0.2)
    monkeypatch.setenv("GRAPHTRAIL_BIN", str(graphtrail))

    assert (
        work_cmd.verify_run(
            target=tmp_path,
            commands=["python3 -c \"print('ok')\""],
            graphtrail_timeout=0.05,
            json_output=True,
        )
        == 0
    )
    receipt = json.loads(capsys.readouterr().out)
    sidecar = json.loads((Path(receipt["path"]) / "graph-delta.json").read_text())
    before_sync = sidecar["commands"]["before_sync"]

    assert receipt["commands"][0]["status"] == "completed"
    assert receipt["commands"][0]["exit_code"] == 0
    assert receipt["code_graph_delta"]["stale_graph_used"] is True
    assert sidecar["stale_graph_used"] is True
    assert before_sync["stage"] == "incremental-sync"
    assert before_sync["timed_out"] is True


def test_work_verify_graphtrail_delta_diff_timeout_is_distinct_from_sync_timeout(tmp_path, capsys, monkeypatch):
    _init_git_repo(tmp_path)
    graphtrail = _write_fake_graphtrail(
        tmp_path,
        diff_delay_seconds=_GRAPHTRAIL_SLOW_STAGE_DELAY_SECONDS,
    )
    monkeypatch.setenv("GRAPHTRAIL_BIN", str(graphtrail))
    _derace_graphtrail_sync_timing(monkeypatch, graphtrail)

    assert (
        work_cmd.verify_run(
            target=tmp_path,
            commands=["python3 -c \"print('ok')\""],
            graphtrail_timeout=_GRAPHTRAIL_TIGHT_TIMEOUT_SECONDS,
            json_output=True,
        )
        == 0
    )
    receipt = json.loads(capsys.readouterr().out)
    sidecar = json.loads((Path(receipt["path"]) / "graph-delta.json").read_text())
    diff_command = sidecar["commands"]["diff"]

    assert receipt["commands"][0]["status"] == "completed"
    assert receipt["commands"][0]["exit_code"] == 0
    assert receipt["code_graph_delta"]["status"] == "diff_timed_out"
    assert sidecar["status"] == "diff_timed_out"
    assert diff_command["stage"] == "diff"
    assert diff_command["timed_out"] is True
    assert diff_command["returncode"] == 124
    assert diff_command["duration_seconds"] >= _GRAPHTRAIL_TIGHT_TIMEOUT_SECONDS
    assert diff_command["stderr"]


def test_work_verify_graphtrail_delta_timeout_differs_from_sync_command_failure(tmp_path, capsys, monkeypatch):
    _init_git_repo(tmp_path)
    graphtrail = _write_fake_graphtrail(tmp_path, mode="sync-fail")
    monkeypatch.setenv("GRAPHTRAIL_BIN", str(graphtrail))

    assert work_cmd.verify_run(target=tmp_path, commands=["python3 -c \"print('ok')\""], json_output=True) == 0
    receipt = json.loads(capsys.readouterr().out)
    sidecar = json.loads((Path(receipt["path"]) / "graph-delta.json").read_text())
    before_sync = sidecar["commands"]["before_sync"]

    assert receipt["code_graph_delta"]["status"] == "sync_failed"
    assert sidecar["status"] == "sync_failed"
    assert before_sync["timed_out"] is False
    assert before_sync["returncode"] == 5
    assert "sync failed" in before_sync["stderr"]


def test_work_verify_run_terminalizes_keyboard_interrupt(tmp_path, capsys, monkeypatch):
    from brigade.work_cmd import verification as verify_mod

    _init_git_repo(tmp_path)
    child_processes: list[subprocess.Popen[bytes]] = []

    def interrupted_child_runner(execution_argv, *, cwd, env, timeout):
        popen_kwargs = verify_mod._verify_child_popen_kwargs()
        process = subprocess.Popen(
            execution_argv,
            cwd=cwd,
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            stdin=subprocess.DEVNULL,
            shell=False,
            **popen_kwargs,
        )
        child_processes.append(process)
        verify_mod.proc._terminate_processes((process,), terminate_grace=0.5, kill_grace=0.5)
        return verify_mod._VERIFY_INTERRUPTED_COMMAND_STATUS, None, "partial-out", ""

    monkeypatch.setattr(verify_mod, "_run_verify_child_process", interrupted_child_runner)

    rc = work_cmd.verify_run(
        target=tmp_path,
        commands=[[sys.executable, "-c", "import time; time.sleep(3600)"]],
        timeout=30,
        json_output=True,
    )
    captured = capsys.readouterr()

    assert rc == 130
    assert "Traceback" not in captured.err
    assert child_processes
    assert all(process.poll() is not None for process in child_processes)

    receipt = json.loads(captured.out)
    assert receipt["status"] == verify_mod._VERIFY_CANCELED_RECEIPT_STATUS
    assert receipt["status"] != "failed"
    assert receipt["status"] != "running"
    assert receipt["completed_at"]
    assert receipt["interruption"]["kind"] == "keyboard-interrupt"
    assert receipt["commands"][0]["status"] == verify_mod._VERIFY_INTERRUPTED_COMMAND_STATUS
    assert receipt["commands"][0]["status"] != "failed"

    receipt_path = Path(receipt["path"]) / "receipt.json"
    assert receipt_path.is_file()
    persisted = json.loads(receipt_path.read_text())
    assert persisted["status"] == verify_mod._VERIFY_CANCELED_RECEIPT_STATUS

    from brigade import outcome_cmd

    assert outcome_cmd.capture(target=tmp_path, artifact_id="brigade-work", json_output=True) == 0
    record = json.loads(capsys.readouterr().out)["record"]
    assert record["signal_value"] == 0


def test_work_verify_run_canceled_status_is_neutral_not_a_regression_signal(tmp_path, capsys):
    from brigade.work_cmd import verification as verify_mod

    _init_git_repo(tmp_path)

    assert (
        work_cmd.verify_run(
            target=tmp_path,
            commands=['python3 -c "raise SystemExit(3)"'],
            json_output=True,
        )
        == 3
    )
    failed_receipt = json.loads(capsys.readouterr().out)
    assert failed_receipt["status"] == "failed"

    from brigade import outcome_cmd

    assert outcome_cmd.capture(target=tmp_path, artifact_id="brigade-work", json_output=True) == 0
    failed_record = json.loads(capsys.readouterr().out)["record"]
    assert failed_record["signal_value"] == -1
    assert verify_mod._VERIFY_CANCELED_RECEIPT_STATUS != failed_receipt["status"]


def test_run_verify_child_process_catches_keyboard_interrupt(tmp_path, monkeypatch):
    from brigade.work_cmd import verification as verify_mod

    real_popen = verify_mod.subprocess.Popen
    child_processes: list[subprocess.Popen[bytes]] = []

    def interrupting_popen(*args, **kwargs):
        process = real_popen(*args, **kwargs)
        child_processes.append(process)

        def communicate(*communicate_args, **communicate_kwargs):
            raise KeyboardInterrupt

        process.communicate = communicate  # type: ignore[method-assign]
        return process

    monkeypatch.setattr(verify_mod.subprocess, "Popen", interrupting_popen)

    status, exit_code, stdout, stderr = verify_mod._run_verify_child_process(
        [sys.executable, "-c", "import time; time.sleep(3600)"],
        cwd=tmp_path,
        env=os.environ.copy(),
        timeout=30,
    )

    assert status == verify_mod._VERIFY_INTERRUPTED_COMMAND_STATUS
    assert exit_code is None
    assert child_processes
    assert all(process.poll() is not None for process in child_processes)


def test_work_verify_run_cli_passes_graphtrail_timeout_override(tmp_path, monkeypatch):
    seen: list[dict[str, object]] = []

    def fake_verify_run(**kwargs):
        seen.append(kwargs)
        return 0

    monkeypatch.setattr(work_cmd, "verify_run", fake_verify_run)

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
                "--graphtrail-timeout",
                "45",
                "--json",
            ]
        )
        == 0
    )
    assert seen == [
        {
            "target": tmp_path,
            "commands": ["python3 -m pytest -q"],
            "manifest_id": None,
            "timeout": 900,
            "graphtrail_timeout": 45,
            "json_output": True,
            "capture": None,
            "capture_kind": "skill",
            "reuse": True,
        }
    ]


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
        (
            "verify-plan",
            {"target": tmp_path, "commands": ["python3 -m pytest -q"], "manifest_id": None, "json_output": True},
        ),
        (
            "verify-run",
            {
                "target": tmp_path,
                "commands": ["python3 -m pytest -q"],
                "manifest_id": None,
                "timeout": 12,
                "graphtrail_timeout": None,
                "json_output": True,
                "capture": None,
                "capture_kind": "skill",
                "reuse": True,
            },
        ),
        ("verify-runs", {"target": tmp_path, "limit": 3, "json_output": True}),
        ("verify-show", {"target": tmp_path, "run_id": "latest", "json_output": True}),
        ("closeout", {"target": tmp_path, "session_id": "latest", "json_output": True}),
    ]


def test_tree_fingerprint_distinguishes_tracked_binary_changes(tmp_path):
    # `git diff HEAD` elides binary content ("Binary files differ"), so two
    # different modifications of a tracked binary would collide without
    # --binary. The fingerprint must include the binary delta.
    from brigade.work_cmd import verification

    target_a = tmp_path / "repo-a"
    target_b = tmp_path / "repo-b"
    env = {
        **os.environ,
        "GIT_AUTHOR_DATE": "2026-01-01T00:00:00Z",
        "GIT_COMMITTER_DATE": "2026-01-01T00:00:00Z",
    }
    for target in (target_a, target_b):
        target.mkdir()
        _init_git_repo_with_fixed_head(target)
        (target / "blob.bin").write_bytes(b"\x00\x01\x02base")
        subprocess.run(["git", "add", "blob.bin"], cwd=target, check=True, stdout=subprocess.DEVNULL)
        subprocess.run(
            ["git", "commit", "-m", "add blob"],
            cwd=target,
            check=True,
            stdout=subprocess.DEVNULL,
            env=env,
        )

    head_a = subprocess.check_output(["git", "-C", str(target_a), "rev-parse", "HEAD"], text=True).strip()
    head_b = subprocess.check_output(["git", "-C", str(target_b), "rev-parse", "HEAD"], text=True).strip()
    assert head_a == head_b, "test setup: HEAD commits must be identical to isolate the binary-diff collision"

    (target_a / "blob.bin").write_bytes(b"\x00\x01\x02changed-one-way")
    (target_b / "blob.bin").write_bytes(b"\x00\x01\x02changed-another")

    fp_a = verification._tree_fingerprint(target_a)
    fp_b = verification._tree_fingerprint(target_b)

    assert fp_a is not None
    assert fp_b is not None
    assert fp_a != fp_b


EMPTY_PATCH_SHA256 = "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"


def _run_verify_with_graphtrail_disabled(target, monkeypatch, *, commands=None, reuse=True):
    from brigade.work_cmd import verification

    monkeypatch.setenv("GRAPHTRAIL_BIN", str(target / "missing-graphtrail"))
    return verification.verify_run(
        target=target,
        commands=commands or ["true"],
        timeout=60,
        reuse=reuse,
        json_output=True,
    )


def test_verify_receipt_identity_same_tree_twice(tmp_target, monkeypatch, capsys):
    _init_verify_target_with_head(tmp_target)
    assert _run_verify_with_graphtrail_disabled(tmp_target, monkeypatch) == 0
    first = json.loads(capsys.readouterr().out)
    assert _run_verify_with_graphtrail_disabled(tmp_target, monkeypatch) == 0
    second = json.loads(capsys.readouterr().out)
    assert first["tree_fingerprint"] == second["tree_fingerprint"]
    assert first["baseline_commit"] == second["baseline_commit"]
    assert first["changes_patch_sha256"] == second["changes_patch_sha256"]


def test_verify_receipt_identity_changed_file_differs(tmp_target, monkeypatch, capsys):
    _init_verify_target_with_head(tmp_target)
    assert _run_verify_with_graphtrail_disabled(tmp_target, monkeypatch) == 0
    clean = json.loads(capsys.readouterr().out)
    (tmp_target / "tracked.txt").write_text("changed\n")
    assert _run_verify_with_graphtrail_disabled(tmp_target, monkeypatch, reuse=False) == 0
    dirty = json.loads(capsys.readouterr().out)
    assert dirty["tree_fingerprint"] != clean["tree_fingerprint"]
    assert dirty["baseline_commit"] == clean["baseline_commit"]
    assert dirty["changes_patch_sha256"] != clean["changes_patch_sha256"]


def test_verify_receipt_identity_ignores_gitignored_untracked(tmp_target, monkeypatch, capsys):
    _init_verify_target_with_head(tmp_target)
    (tmp_target / ".gitignore").write_text(".brigade/\nignored.txt\n")
    subprocess.run(["git", "add", ".gitignore"], cwd=tmp_target, check=True, stdout=subprocess.DEVNULL)
    subprocess.run(
        ["git", "-c", "user.name=Test User", "-c", "user.email=test@example.invalid", "commit", "-m", "ignore"],
        cwd=tmp_target,
        check=True,
        stdout=subprocess.DEVNULL,
    )
    assert _run_verify_with_graphtrail_disabled(tmp_target, monkeypatch) == 0
    baseline = json.loads(capsys.readouterr().out)
    (tmp_target / "ignored.txt").write_text("secret\n")
    (tmp_target / "visible.txt").write_text("visible\n")
    assert _run_verify_with_graphtrail_disabled(tmp_target, monkeypatch, reuse=False) == 0
    with_visible = json.loads(capsys.readouterr().out)
    patch_text = Path(with_visible["path"], "changes.patch").read_text()
    assert "visible.txt" in patch_text
    assert "ignored.txt" not in patch_text
    (tmp_target / "visible.txt").unlink()
    assert _run_verify_with_graphtrail_disabled(tmp_target, monkeypatch, reuse=False) == 0
    ignored_only = json.loads(capsys.readouterr().out)
    assert ignored_only["tree_fingerprint"] == baseline["tree_fingerprint"]
    assert ignored_only["changes_patch_sha256"] == baseline["changes_patch_sha256"]


def test_verify_receipt_identity_includes_unignored_untracked(tmp_target, monkeypatch, capsys):
    _init_verify_target_with_head(tmp_target)
    (tmp_target / "new.txt").write_text("fresh\n")
    assert _run_verify_with_graphtrail_disabled(tmp_target, monkeypatch, reuse=False) == 0
    receipt = json.loads(capsys.readouterr().out)
    patch_text = Path(receipt["path"], "changes.patch").read_text()
    assert "new.txt" in patch_text
    assert receipt["changes_patch_sha256"] == hashlib.sha256(patch_text.encode()).hexdigest()


def test_verify_receipt_changes_patch_sha256_matches_file(tmp_target, monkeypatch, capsys):
    _init_verify_target_with_head(tmp_target)
    (tmp_target / "delta.txt").write_text("delta\n")
    assert _run_verify_with_graphtrail_disabled(tmp_target, monkeypatch, reuse=False) == 0
    receipt = json.loads(capsys.readouterr().out)
    patch_bytes = (Path(receipt["path"]) / "changes.patch").read_bytes()
    assert receipt["changes_patch_sha256"] == hashlib.sha256(patch_bytes).hexdigest()


def test_verify_receipt_clean_tree_empty_patch(tmp_target, monkeypatch, capsys):
    _init_verify_target_with_head(tmp_target)
    assert _run_verify_with_graphtrail_disabled(tmp_target, monkeypatch) == 0
    receipt = json.loads(capsys.readouterr().out)
    patch_path = Path(receipt["path"]) / "changes.patch"
    assert patch_path.is_file()
    assert patch_path.read_bytes() == b""
    assert receipt["changes_patch_sha256"] == EMPTY_PATCH_SHA256


def test_verify_receipt_schema_version_two(tmp_target, monkeypatch, capsys):
    _init_verify_target_with_head(tmp_target)
    assert _run_verify_with_graphtrail_disabled(tmp_target, monkeypatch) == 0
    receipt = json.loads(capsys.readouterr().out)
    assert receipt["schema_version"] == 2


def test_verify_show_old_format_receipt_without_identity_fields(tmp_target, capsys):
    from brigade.work_cmd import helpers, verification

    _init_verify_target_with_head(tmp_target)
    run_dir = helpers._verify_runs_root(tmp_target) / "20260101-000000-work-verify-legacy"
    run_dir.mkdir(parents=True)
    legacy = {
        "run_id": run_dir.name,
        "target": str(tmp_target),
        "status": "completed",
        "started_at": "2026-01-01T00:00:00+00:00",
        "completed_at": "2026-01-01T00:00:01+00:00",
        "path": str(run_dir),
        "commands": [{"command": "true", "status": "completed", "exit_code": 0}],
    }
    helpers._write_json(run_dir / "receipt.json", legacy)
    assert verification.verify_show(target=tmp_target, run_id=run_dir.name) == 0
    out = capsys.readouterr().out
    assert "baseline" not in out.lower()
    assert "verified tree" not in out


def test_verify_show_renders_identity_binding(tmp_target, monkeypatch, capsys):
    from brigade.work_cmd import verification

    _init_verify_target_with_head(tmp_target)
    assert _run_verify_with_graphtrail_disabled(tmp_target, monkeypatch) == 0
    receipt = json.loads(capsys.readouterr().out)
    assert verification.verify_show(target=tmp_target, run_id=receipt["run_id"]) == 0
    out = capsys.readouterr().out
    assert f"baseline commit: {receipt['baseline_commit']}" in out
    assert f"tree fingerprint: {receipt['tree_fingerprint']}" in out
    assert f"patch hash: {receipt['changes_patch_sha256']}" in out
    assert (
        f"verified tree {receipt['tree_fingerprint']} = baseline {receipt['baseline_commit']} + patch {receipt['changes_patch_sha256']}"
        in out
    )


def test_verify_markdown_renders_identity_binding(tmp_target, monkeypatch, capsys):
    _init_verify_target_with_head(tmp_target)
    assert _run_verify_with_graphtrail_disabled(tmp_target, monkeypatch) == 0
    receipt = json.loads(capsys.readouterr().out)
    summary = Path(receipt["path"], "summary.md").read_text()
    assert f"baseline {receipt['baseline_commit']}" in summary.lower()
    assert receipt["tree_fingerprint"] in summary
    assert receipt["changes_patch_sha256"] in summary
    assert (
        f"verified tree {receipt['tree_fingerprint']} = baseline {receipt['baseline_commit']} + patch {receipt['changes_patch_sha256']}"
        in summary
    )


def test_verify_reused_receipt_captures_identity_binding(tmp_target, monkeypatch, capsys):
    _init_verify_target_with_head(tmp_target)
    assert _run_verify_with_graphtrail_disabled(tmp_target, monkeypatch) == 0
    capsys.readouterr()
    assert _run_verify_with_graphtrail_disabled(tmp_target, monkeypatch) == 0
    reused = json.loads(capsys.readouterr().out)
    assert reused.get("reused_from")
    assert reused["schema_version"] == 2
    assert reused["baseline_commit"]
    assert reused["tree_fingerprint"]
    assert reused["changes_patch_sha256"] == EMPTY_PATCH_SHA256
    patch_path = Path(reused["path"]) / "changes.patch"
    assert patch_path.is_file()
    assert patch_path.read_bytes() == b""
    assert (
        f"verified tree {reused['tree_fingerprint']} = baseline {reused['baseline_commit']} + patch {reused['changes_patch_sha256']}"
        in (Path(reused["path"]) / "summary.md").read_text()
    )


def test_verify_receipt_identity_patch_collection_failure_is_explicit(tmp_target, monkeypatch, capsys):
    from brigade import runguard

    _init_verify_target_with_head(tmp_target)

    def fail_patch_collection(*_args, **_kwargs):
        raise runguard.RunGuardError("simulated patch collection failure")

    monkeypatch.setattr(runguard, "collect_changes_patch", fail_patch_collection)
    assert _run_verify_with_graphtrail_disabled(tmp_target, monkeypatch, reuse=False) == 0
    receipt = json.loads(capsys.readouterr().out)
    assert receipt["schema_version"] == 2
    assert receipt["baseline_commit"] is None
    assert receipt["tree_fingerprint"] is None
    assert receipt["changes_patch_sha256"] is None


def test_verify_receipt_identity_preserves_real_index(tmp_target, monkeypatch, capsys):
    _init_verify_target_with_head(tmp_target)
    (tmp_target / "tracked.txt").write_text("staged\n")
    subprocess.run(
        ["git", "add", "tracked.txt"],
        cwd=tmp_target,
        check=True,
        stdout=subprocess.DEVNULL,
    )
    before = subprocess.run(
        ["git", "diff", "--cached", "--binary"],
        cwd=tmp_target,
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    assert _run_verify_with_graphtrail_disabled(tmp_target, monkeypatch, reuse=False) == 0
    capsys.readouterr()
    after = subprocess.run(
        ["git", "diff", "--cached", "--binary"],
        cwd=tmp_target,
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    assert after == before


def test_archive_verify_run_strips_recovery_checkpoint_bodies(tmp_path):
    """Export boundary: verify-archive must not emit private checkpoint bodies (#636)."""
    from brigade import run_checkpoint
    from brigade.work_cmd import helpers, verification

    root = helpers._verify_runs_root(tmp_path)
    root.mkdir(parents=True)
    run_dir, _ = _write_verify_run_dir(root, "20260101-000001-a", sign=True)
    _write_verify_run_dir(root, "20260101-000002-b")

    private_task = "SECRET_CHECKPOINT_TASK_must_not_archive"
    body_obj = {"schema": "brigade.run.v1", "status": "failed", "task": private_task, "error": "boom"}
    body = (json.dumps(body_obj, indent=2, sort_keys=True) + "\n").encode("utf-8")
    cp_dir = run_dir / "events" / "recovery-checkpoints"
    cp_dir.mkdir(parents=True)
    sha = hashlib.sha256(body).hexdigest()
    (cp_dir / f"{sha}.json").write_bytes(body)
    os.chmod(cp_dir / f"{sha}.json", 0o600)

    archive_root = tmp_path / "verify-archive"
    removed = verification._prune_verify_runs(tmp_path, keep=1, archive_root=archive_root)

    assert removed == 1
    archived_cp = archive_root / "20260101-000001-a" / "events" / "recovery-checkpoints" / f"{sha}.json"
    assert archived_cp.is_file()
    payload = json.loads(archived_cp.read_text(encoding="utf-8"))
    assert run_checkpoint.is_checkpoint_artifact_reference(payload)
    assert private_task not in archived_cp.read_text(encoding="utf-8")
    assert '"task"' not in archived_cp.read_text(encoding="utf-8")
    # Source verify-run is deleted after archive; the privacy rule is on the export.
    assert not (root / "20260101-000001-a").exists()


def test_capture_before_retry_uses_receipt_stamped_artifact(tmp_target, monkeypatch, capsys):
    from brigade.work_cmd import verification

    _init_verify_target_with_head(tmp_target)
    monkeypatch.setenv("GRAPHTRAIL_BIN", str(tmp_target / "missing-graphtrail"))
    skill_dir = tmp_target / ".claude" / "skills" / "taste"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text("# taste\n")

    verification.verify_run(target=tmp_target, commands=["false"], timeout=60, capture="taste")
    failed = verification._verify_receipts(tmp_target)[0]
    assert failed["outcome_capture"]["artifact_id"] == "taste"
    # Drop the auto-captured ledger row so retry enforcement still fires, while
    # the receipt keeps the stamped capture intent.
    records = tmp_target / "memory" / "outcome" / "records.jsonl"
    if records.is_file():
        records.write_text("")
    capsys.readouterr()
    rc = verification.verify_run(target=tmp_target, commands=["false"], timeout=60)
    assert rc != 0
    err = capsys.readouterr().err
    assert f"warning: brigade outcome capture taste --run-id {failed['run_id']}" in err
    assert "capture brigade-work" not in err


def test_capture_before_retry_prefers_receipt_stamp_over_current_capture(tmp_target, monkeypatch, capsys):
    from brigade.work_cmd import verification

    _init_verify_target_with_head(tmp_target)
    monkeypatch.setenv("GRAPHTRAIL_BIN", str(tmp_target / "missing-graphtrail"))
    skill_dir = tmp_target / ".claude" / "skills" / "taste"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text("# taste\n")

    verification.verify_run(target=tmp_target, commands=["false"], timeout=60, capture="taste")
    failed = verification._verify_receipts(tmp_target)[0]
    records = tmp_target / "memory" / "outcome" / "records.jsonl"
    if records.is_file():
        records.write_text("")
    capsys.readouterr()

    rc = verification.verify_run(target=tmp_target, commands=["false"], timeout=60, capture="refire")

    assert rc != 0
    err = capsys.readouterr().err
    assert f"warning: brigade outcome capture taste --run-id {failed['run_id']}" in err
    assert f"warning: brigade outcome capture refire --run-id {failed['run_id']}" not in err


def test_capture_before_retry_uses_current_capture_when_receipt_unstamped(tmp_target, monkeypatch, capsys):
    from brigade.work_cmd import verification

    _init_verify_target_with_head(tmp_target)
    monkeypatch.setenv("GRAPHTRAIL_BIN", str(tmp_target / "missing-graphtrail"))

    verification.verify_run(target=tmp_target, commands=["false"], timeout=60)
    failed = verification._verify_receipts(tmp_target)[0]
    rc = verification.verify_run(target=tmp_target, commands=["false"], timeout=60, capture="refire")
    assert rc != 0
    err = capsys.readouterr().err
    assert f"warning: brigade outcome capture refire --run-id {failed['run_id']}" in err


def test_capture_before_retry_falls_back_to_brigade_work(tmp_target, monkeypatch, capsys):
    from brigade.work_cmd import verification

    _init_verify_target_with_head(tmp_target)
    monkeypatch.setenv("GRAPHTRAIL_BIN", str(tmp_target / "missing-graphtrail"))

    verification.verify_run(target=tmp_target, commands=["false"], timeout=60)
    failed = verification._verify_receipts(tmp_target)[0]
    verification.verify_run(target=tmp_target, commands=["false"], timeout=60)
    err = capsys.readouterr().err
    assert f"warning: brigade outcome capture brigade-work --run-id {failed['run_id']}" in err


def test_distinct_skill_captures_accumulate_separate_rank_signals(tmp_target, monkeypatch, capsys):
    from brigade import outcome_cmd
    from brigade.work_cmd import verification

    _init_verify_target_with_head(tmp_target)
    monkeypatch.setenv("GRAPHTRAIL_BIN", str(tmp_target / "missing-graphtrail"))
    for skill in ("taste", "refire"):
        skill_dir = tmp_target / ".claude" / "skills" / skill
        skill_dir.mkdir(parents=True)
        (skill_dir / "SKILL.md").write_text(f"# {skill}\n")

    assert verification.verify_run(target=tmp_target, commands=["true"], timeout=60, capture="taste") == 0
    (tmp_target / "touch.txt").write_text("change\n")
    assert verification.verify_run(target=tmp_target, commands=["true"], timeout=60, capture="refire", reuse=False) == 0
    capsys.readouterr()
    assert outcome_cmd.rank(target=tmp_target, json_output=True) == 0
    payload = json.loads(capsys.readouterr().out)
    by_id = {entry["artifact_id"]: entry for entry in payload["ranking"]}
    assert by_id["taste"]["helped"] == 1
    assert by_id["refire"]["helped"] == 1
    assert "brigade-work" not in by_id or by_id.get("brigade-work", {}).get("helped", 0) == 0
