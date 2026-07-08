import json
import subprocess
import sys
from pathlib import Path

from brigade import cli, localio, outcome, outcome_cmd, receipt_signing, receipts_cmd, runbook_cmd, work_cmd

from tests.work_cmd_test_helpers import _init_git_repo


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
    return subprocess.check_output(["git", "-C", str(path), "rev-parse", "HEAD"], text=True).strip()


def _write_digestless_work_receipt(target):
    run_dir = target / ".brigade" / "work" / "verify-runs" / "legacy"
    run_dir.mkdir(parents=True)
    receipt = {
        "run_id": "legacy",
        "target": str(target),
        "status": "completed",
        "commands": [],
    }
    localio.write_json(run_dir / "receipt.json", receipt)


def _write_runbook(path):
    path.write_text(
        json.dumps(
            {
                "id": "smoke",
                "description": "tiny runbook",
                "allowed_commands": ["printf"],
                "steps": [{"id": "hello", "run": "printf hello"}],
            }
        )
    )
    return path


def _append_outcome(target, evidence_ref):
    outcome_cmd.append_records(
        target,
        [
            outcome.OutcomeRecord(
                "taste",
                "skill",
                evidence_ref,
                "verify",
                1,
                evidence_ref,
                f"2026-07-08T00:00:0{evidence_ref[-1]}+00:00",
            )
        ],
    )


def _receipt_digest(payload):
    return localio.canonical_json_digest(payload, exclude_keys={"digests"})


def _write_verify_export_receipt(
    target,
    run_id,
    *,
    started_at,
    digest=True,
    digest_signature=None,
    code_graph_delta=None,
    git=None,
):
    run_dir = target / ".brigade" / "work" / "verify-runs" / run_id
    run_dir.mkdir(parents=True)
    stdout = run_dir / "command-1-stdout.log"
    stderr = run_dir / "command-1-stderr.log"
    stdout.write_text("ok\n")
    stderr.write_text("")
    receipt = {
        "run_id": run_id,
        "target": str(target),
        "status": "completed",
        "started_at": started_at,
        "completed_at": started_at.replace("00Z", "05Z"),
        "commands": [
            {
                "command": "python3 -c \"print('ok')\"",
                "status": "completed",
                "exit_code": 0,
                "stdout_log_path": str(stdout),
                "stderr_log_path": str(stderr),
            }
        ],
    }
    if code_graph_delta is not None:
        sidecar = run_dir / "graph-delta.json"
        sidecar.write_text(json.dumps(code_graph_delta, sort_keys=True) + "\n")
        receipt["code_graph_delta"] = code_graph_delta
    if git is not None:
        receipt["git"] = git
    if digest:
        logs = {
            "command-1-stderr.log": localio.file_sha256(stderr),
            "command-1-stdout.log": localio.file_sha256(stdout),
        }
        if code_graph_delta is not None:
            logs["graph-delta.json"] = localio.file_sha256(run_dir / "graph-delta.json")
        receipt["digests"] = {
            "algorithm": "sha256",
            "logs": dict(sorted(logs.items())),
            "receipt_sha256": _receipt_digest(receipt),
        }
        if digest_signature is not None:
            receipt["digests"]["signature"] = digest_signature["signature"]
            receipt["digests"]["key_id"] = digest_signature["key_id"]
    localio.write_json(run_dir / "receipt.json", receipt)
    return run_dir / "receipt.json"


def _write_run_export_receipt(target, run_id, *, started_at, digest=False, code_graph_delta=None):
    run_dir = target / ".brigade" / "runs" / run_id
    run_dir.mkdir(parents=True)
    payload = {
        "task": "export receipts",
        "cwd": str(target),
        "orchestrator": "planner",
        "dry_run": False,
        "read_only": False,
        "status": "ok",
        "started_at": started_at,
        "finished_at": started_at.replace("00Z", "07Z"),
        "artifacts": str(run_dir),
    }
    if code_graph_delta is not None:
        payload["code_graph_delta"] = code_graph_delta
    if digest:
        payload["digests"] = {
            "algorithm": "sha256",
            "receipt_sha256": _receipt_digest(payload),
        }
    localio.write_json(run_dir / "run.json", payload)
    return run_dir / "run.json"


def _jsonl(text):
    return [json.loads(line) for line in text.splitlines() if line.strip()]


def test_receipts_export_miseledger_emits_required_verify_fields_and_artifacts(tmp_path, capsys):
    receipt_path = _write_verify_export_receipt(
        tmp_path,
        "20260708-120000-work-verify-abc123",
        started_at="2026-07-08T12:00:00Z",
        digest=True,
        code_graph_delta={"status": "ok", "summary": "edge_churn=2", "changed_symbol_count": 2},
    )

    assert cli.main(["receipts", "export", "miseledger", "--target", str(tmp_path)]) == 0
    rows = _jsonl(capsys.readouterr().out)

    assert len(rows) == 1
    row = rows[0]
    assert row["schema"] == "miseledger.adapter.v1"
    assert row["source"]["kind"] == "brigade"
    assert row["collection"]["external_id"] == "brigade:work:verify-runs"
    assert row["collection"]["kind"] == "brigade_work_verify_runs"
    assert row["item"]["external_id"] == "brigade:work-verify:20260708-120000-work-verify-abc123"
    assert row["item"]["kind"] == "brigade_work_verify_receipt"
    assert row["actor"] == {"external_id": "brigade:system", "type": "system", "name": "Brigade"}
    assert "edge_churn=2" in row["item"]["text"]
    assert row["item"]["metadata"]["code_graph_delta_summary"] == "edge_churn=2"
    assert row["item"]["metadata"]["code_graph_delta"]["changed_symbol_count"] == 2
    assert row["raw"]["path"] == ".brigade/work/verify-runs/20260708-120000-work-verify-abc123/receipt.json"
    assert row["raw"]["hash"] == "sha256:" + json.loads(receipt_path.read_text())["digests"]["receipt_sha256"]
    assert row["raw"]["ordinal"] == 1
    assert all(
        set(artifact) <= {"external_id", "kind", "path", "url", "mime_type", "text", "hash", "metadata"}
        for artifact in row["artifacts"]
    )
    artifact_paths = {artifact["path"] for artifact in row["artifacts"]}
    assert ".brigade/work/verify-runs/20260708-120000-work-verify-abc123/receipt.json" in artifact_paths
    assert ".brigade/work/verify-runs/20260708-120000-work-verify-abc123/command-1-stdout.log" in artifact_paths
    assert ".brigade/work/verify-runs/20260708-120000-work-verify-abc123/graph-delta.json" in artifact_paths
    assert row["artifacts"][0]["hash"].startswith("sha256:")
    assert row["links"] == []
    assert row["relations"] == []


def test_receipts_export_miseledger_exports_run_and_digestless_verify_receipts(tmp_path, capsys):
    verify_path = _write_verify_export_receipt(
        tmp_path,
        "20260708-110000-work-verify-def456",
        started_at="2026-07-08T11:00:00Z",
        digest=False,
    )
    run_path = _write_run_export_receipt(
        tmp_path,
        "20260708-130000-aabbccdd",
        started_at="2026-07-08T13:00:00Z",
        digest=True,
        code_graph_delta={"status": "ok", "summary": "changed_symbols=1"},
    )

    assert receipts_cmd.export_miseledger(target=tmp_path) == 0
    rows = _jsonl(capsys.readouterr().out)

    assert [row["item"]["kind"] for row in rows] == [
        "brigade_run_receipt",
        "brigade_work_verify_receipt",
    ]
    assert rows[0]["collection"]["external_id"] == "brigade:runs"
    assert rows[0]["item"]["external_id"] == "brigade:run:20260708-130000-aabbccdd"
    assert rows[0]["raw"]["hash"] == "sha256:" + json.loads(run_path.read_text())["digests"]["receipt_sha256"]
    assert rows[0]["item"]["metadata"]["code_graph_delta_summary"] == "changed_symbols=1"
    assert rows[1]["raw"]["hash"] == "sha256:" + localio.file_sha256(verify_path)
    assert rows[1]["item"]["metadata"]["digest_source"] == "file_sha256"


def test_receipts_export_miseledger_includes_digest_signature_when_present(tmp_path, capsys):
    signature = {"signature": "a" * 64, "key_id": "deadbeef"}
    _write_verify_export_receipt(
        tmp_path,
        "20260708-110000-work-verify-signed",
        started_at="2026-07-08T11:00:00Z",
        digest_signature=signature,
    )

    assert receipts_cmd.export_miseledger(target=tmp_path) == 0
    rows = _jsonl(capsys.readouterr().out)

    assert rows[0]["item"]["metadata"]["digest_signature"] == signature


def test_receipts_export_miseledger_omits_digest_signature_when_absent(tmp_path, capsys):
    _write_verify_export_receipt(
        tmp_path,
        "20260708-110000-work-verify-unsigned",
        started_at="2026-07-08T11:00:00Z",
    )

    assert receipts_cmd.export_miseledger(target=tmp_path) == 0
    rows = _jsonl(capsys.readouterr().out)

    assert "digest_signature" not in rows[0]["item"]["metadata"]


def test_receipts_export_miseledger_is_byte_identical_and_limit_uses_newest_first(tmp_path, capsys):
    _write_verify_export_receipt(
        tmp_path,
        "20260708-090000-work-verify-old",
        started_at="2026-07-08T09:00:00Z",
    )
    _write_run_export_receipt(tmp_path, "20260708-140000-new", started_at="2026-07-08T14:00:00Z")

    assert cli.main(["receipts", "export", "miseledger", "--target", str(tmp_path), "--limit", "1"]) == 0
    first = capsys.readouterr().out
    assert cli.main(["receipts", "export", "miseledger", "--target", str(tmp_path), "--limit", "1"]) == 0
    second = capsys.readouterr().out

    assert first == second
    rows = _jsonl(first)
    assert [row["item"]["external_id"] for row in rows] == ["brigade:run:20260708-140000-new"]


def test_receipts_export_miseledger_new_only_exports_once_and_records_cursor(tmp_path, capsys):
    _write_verify_export_receipt(
        tmp_path,
        "20260708-090000-work-verify-old",
        started_at="2026-07-08T09:00:00Z",
    )
    _write_run_export_receipt(tmp_path, "20260708-140000-new", started_at="2026-07-08T14:00:00Z")

    assert cli.main(["receipts", "export", "miseledger", "--target", str(tmp_path), "--new-only"]) == 0
    first_rows = _jsonl(capsys.readouterr().out)
    assert len(first_rows) == 2

    cursor_path = tmp_path / ".brigade" / "work" / "miseledger-export-cursor.json"
    cursor = json.loads(cursor_path.read_text())
    assert cursor["raw_hashes"] == sorted(row["raw"]["hash"] for row in first_rows)

    assert cli.main(["receipts", "export", "miseledger", "--target", str(tmp_path), "--new-only"]) == 0
    second = capsys.readouterr()
    assert second.out == ""
    assert second.err == ""
    assert json.loads(cursor_path.read_text()) == cursor


def test_receipts_export_miseledger_without_new_only_ignores_cursor(tmp_path, capsys):
    _write_verify_export_receipt(
        tmp_path,
        "20260708-090000-work-verify-old",
        started_at="2026-07-08T09:00:00Z",
    )
    cursor_path = tmp_path / ".brigade" / "work" / "miseledger-export-cursor.json"
    cursor_path.parent.mkdir(parents=True, exist_ok=True)
    cursor_path.write_text("{not json\n")

    assert receipts_cmd.export_miseledger(target=tmp_path) == 0
    rows = _jsonl(capsys.readouterr().out)

    assert len(rows) == 1
    assert cursor_path.read_text() == "{not json\n"


def test_receipts_export_miseledger_new_only_cursor_records_only_written_lines(tmp_path, monkeypatch, capsys):
    _write_verify_export_receipt(
        tmp_path,
        "20260708-120000-work-verify-first",
        started_at="2026-07-08T12:00:00Z",
    )
    _write_run_export_receipt(tmp_path, "20260708-130000-second", started_at="2026-07-08T13:00:00Z")
    out_path = tmp_path / "export.jsonl"
    writes = []

    class PartialWrite:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def write(self, line):
            writes.append(line)
            if len(writes) == 2:
                raise OSError("disk full")
            return len(line)

    original_open = Path.open

    def partial_open(self, *args, **kwargs):
        mode = args[0] if args else kwargs.get("mode", "r")
        if self == out_path and "w" in mode:
            return PartialWrite()
        return original_open(self, *args, **kwargs)

    monkeypatch.setattr(Path, "open", partial_open)

    assert receipts_cmd.export_miseledger(target=tmp_path, out=out_path, new_only=True) == 1
    captured = capsys.readouterr()

    assert "could not write output" in captured.err
    assert len(writes) == 2
    first_hash = json.loads(writes[0])["raw"]["hash"]
    cursor = json.loads((tmp_path / ".brigade" / "work" / "miseledger-export-cursor.json").read_text())
    assert cursor["raw_hashes"] == [first_hash]


def test_receipts_export_miseledger_import_runs_fake_binary_and_prints_summary(tmp_path, monkeypatch, capsys):
    _write_verify_export_receipt(
        tmp_path,
        "20260708-120000-work-verify-import",
        started_at="2026-07-08T12:00:00Z",
    )
    fake = tmp_path / "miseledger"
    fake.write_text(
        f"""#!{sys.executable}
import json
import pathlib
import sys

assert sys.argv[1:3] == ["import", "adapter"]
export_path = pathlib.Path(sys.argv[3])
assert sys.argv[4:] == ["--source", "brigade", "--json"]
(export_path.parent / "import-argv.json").write_text(json.dumps({{"argv": sys.argv[1:], "input": export_path.read_text()}}))
print(json.dumps({{"inserted_items": 1, "already_known": 0}}))
"""
    )
    fake.chmod(0o755)
    monkeypatch.setenv("PATH", str(tmp_path))

    assert cli.main(["receipts", "export", "miseledger", "--target", str(tmp_path), "--import"]) == 0
    captured = capsys.readouterr()

    assert captured.out == "miseledger import: inserted_items=1 already_known=0\n"
    assert captured.err == ""
    marker = json.loads((tmp_path / ".brigade" / "work" / "import-argv.json").read_text())
    assert marker["argv"][0:2] == ["import", "adapter"]
    assert marker["argv"][3:] == ["--source", "brigade", "--json"]
    assert len(_jsonl(marker["input"])) == 1


def test_receipts_export_miseledger_import_missing_binary_warns_and_exits_zero(tmp_path, monkeypatch, capsys):
    _write_verify_export_receipt(
        tmp_path,
        "20260708-120000-work-verify-missing-import",
        started_at="2026-07-08T12:00:00Z",
    )
    out_path = tmp_path / "export.jsonl"
    empty_path = tmp_path / "empty-path"
    empty_path.mkdir()
    monkeypatch.setenv("PATH", str(empty_path))

    assert receipts_cmd.export_miseledger(target=tmp_path, out=out_path, import_miseledger=True) == 0
    captured = capsys.readouterr()

    assert captured.out == ""
    assert "warning: miseledger binary not found on PATH; export kept at" in captured.err
    assert len(_jsonl(out_path.read_text())) == 1


def test_receipts_export_miseledger_import_skips_binary_on_zero_item_export(tmp_path, monkeypatch, capsys):
    _write_verify_export_receipt(
        tmp_path,
        "20260708-120000-work-verify-nothing-new",
        started_at="2026-07-08T12:00:00Z",
    )
    fake = tmp_path / "miseledger"
    fake.write_text(
        f"""#!{sys.executable}
import pathlib
import sys

pathlib.Path(sys.argv[3]).parent.joinpath("import-argv.json").write_text("invoked")
"""
    )
    fake.chmod(0o755)
    monkeypatch.setenv("PATH", str(tmp_path))

    assert cli.main(["receipts", "export", "miseledger", "--target", str(tmp_path), "--new-only"]) == 0
    assert len(_jsonl(capsys.readouterr().out)) == 1

    assert cli.main(["receipts", "export", "miseledger", "--target", str(tmp_path), "--new-only", "--import"]) == 0
    captured = capsys.readouterr()

    assert captured.out == "nothing new; import skipped\n"
    assert captured.err == ""
    work_dir = tmp_path / ".brigade" / "work"
    assert not (work_dir / "import-argv.json").exists()
    assert list(work_dir.glob("miseledger-export-*.jsonl")) == []


def test_receipts_export_miseledger_copies_git_metadata_and_github_commit_link(tmp_path, capsys):
    head = _init_git_repo_with_head(tmp_path)
    subprocess.run(
        ["git", "remote", "add", "origin", "https://github.com/example-org/example-repo.git"],
        cwd=tmp_path,
        check=True,
    )
    git = {"head": head, "branch": "main", "dirty_files": 2}
    _write_verify_export_receipt(
        tmp_path,
        "20260708-120000-work-verify-git",
        started_at="2026-07-08T12:00:00Z",
        git=git,
    )

    assert receipts_cmd.export_miseledger(target=tmp_path) == 0
    rows = _jsonl(capsys.readouterr().out)

    assert rows[0]["item"]["metadata"]["git"] == git
    assert rows[0]["links"] == [
        {
            "external_id": "brigade:work-verify:20260708-120000-work-verify-git:git-commit",
            "kind": "url",
            "url": f"https://github.com/example-org/example-repo/commit/{head}",
        }
    ]


def test_receipts_export_miseledger_supports_ssh_github_commit_link(tmp_path, capsys):
    head = _init_git_repo_with_head(tmp_path)
    subprocess.run(
        ["git", "remote", "add", "origin", "git@github.com:example-org/example-repo.git"],
        cwd=tmp_path,
        check=True,
    )
    _write_verify_export_receipt(
        tmp_path,
        "20260708-120000-work-verify-ssh-git",
        started_at="2026-07-08T12:00:00Z",
        git={"head": head, "branch": "main", "dirty_files": 0},
    )

    assert receipts_cmd.export_miseledger(target=tmp_path) == 0
    rows = _jsonl(capsys.readouterr().out)

    assert rows[0]["links"][0]["url"] == f"https://github.com/example-org/example-repo/commit/{head}"


def test_receipts_export_miseledger_keeps_git_metadata_without_non_github_link(tmp_path, capsys):
    head = _init_git_repo_with_head(tmp_path)
    subprocess.run(
        ["git", "remote", "add", "origin", "https://example.invalid/example-org/example-repo.git"],
        cwd=tmp_path,
        check=True,
    )
    git = {"head": head, "branch": "main", "dirty_files": 0}
    _write_verify_export_receipt(
        tmp_path,
        "20260708-120000-work-verify-no-link",
        started_at="2026-07-08T12:00:00Z",
        git=git,
    )

    assert receipts_cmd.export_miseledger(target=tmp_path) == 0
    rows = _jsonl(capsys.readouterr().out)

    assert rows[0]["item"]["metadata"]["git"] == git
    assert rows[0]["links"] == []


def test_receipts_export_miseledger_skips_malformed_receipts_with_warning(tmp_path, capsys):
    _write_verify_export_receipt(
        tmp_path,
        "20260708-150000-work-verify-good",
        started_at="2026-07-08T15:00:00Z",
    )
    bad_dir = tmp_path / ".brigade" / "work" / "verify-runs" / "20260708-160000-work-verify-bad"
    bad_dir.mkdir(parents=True)
    (bad_dir / "receipt.json").write_text("{not json\n")

    assert receipts_cmd.export_miseledger(target=tmp_path) == 0
    captured = capsys.readouterr()
    rows = _jsonl(captured.out)

    assert len(rows) == 1
    assert "warning: skipped malformed receipt" in captured.err
    assert "20260708-160000-work-verify-bad" in captured.err


def test_receipts_export_miseledger_empty_target_exits_one(tmp_path, capsys):
    assert cli.main(["receipts", "export", "miseledger", "--target", str(tmp_path)]) == 1
    captured = capsys.readouterr()

    assert captured.out == ""
    assert "no receipts found" in captured.err


def test_receipts_verify_fresh_target_passes(tmp_path, capsys):
    _init_git_repo(tmp_path)
    assert (
        work_cmd.verify_run(
            target=tmp_path,
            commands=["python3 -c \"print('ok')\""],
            timeout=30,
            json_output=True,
        )
        == 0
    )
    capsys.readouterr()
    assert (
        runbook_cmd.run(
            target=tmp_path,
            runbook=_write_runbook(tmp_path / "runbook.json"),
            approved=True,
            json_output=True,
        )
        == 0
    )
    capsys.readouterr()
    _append_outcome(tmp_path, "ref1")

    assert cli.main(["receipts", "verify", "--target", str(tmp_path), "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)

    assert payload["summary"]["mismatch"] == 0
    assert payload["summary"]["missing"] == 0
    assert payload["summary"]["ok"] >= 2
    assert {item["status"] for item in payload["artifacts"]} == {"OK"}
    assert "runbook-receipt" in {item["artifact_type"] for item in payload["artifacts"]}


def test_receipts_verify_reports_mismatch_when_receipt_field_is_edited(tmp_path, capsys):
    _init_git_repo(tmp_path)
    assert (
        work_cmd.verify_run(
            target=tmp_path,
            commands=["python3 -c \"print('ok')\""],
            timeout=30,
            json_output=True,
        )
        == 0
    )
    receipt = json.loads(capsys.readouterr().out)
    path = tmp_path / ".brigade" / "work" / "verify-runs" / receipt["run_id"] / "receipt.json"
    tampered = json.loads(path.read_text())
    tampered["status"] = "failed"
    localio.write_json(path, tampered)

    assert receipts_cmd.verify(target=tmp_path, json_output=True) == 1
    payload = json.loads(capsys.readouterr().out)

    assert payload["summary"]["mismatch"] == 1
    problem = [item for item in payload["artifacts"] if item["status"] == "MISMATCH"][0]
    assert problem["artifact_type"] == "work-verify-receipt"
    assert problem["check"] == "receipt_sha256"


def test_receipts_verify_reports_signed_ok_with_matching_local_key(tmp_path, capsys):
    _init_git_repo(tmp_path)
    receipt_signing.generate_key(tmp_path)
    assert (
        work_cmd.verify_run(
            target=tmp_path,
            commands=["python3 -c \"print('ok')\""],
            timeout=30,
            json_output=True,
        )
        == 0
    )
    capsys.readouterr()

    assert receipts_cmd.verify(target=tmp_path, json_output=True) == 0
    payload = json.loads(capsys.readouterr().out)

    signed_items = [item for item in payload["artifacts"] if item["status"] == "SIGNED-OK"]
    assert len(signed_items) == 1
    assert signed_items[0]["check"] == "digest_signature"


def test_receipts_verify_signature_mismatch_exits_nonzero_when_digest_is_rewritten(tmp_path, capsys):
    _init_git_repo(tmp_path)
    receipt_signing.generate_key(tmp_path)
    assert (
        work_cmd.verify_run(
            target=tmp_path,
            commands=["python3 -c \"print('ok')\""],
            timeout=30,
            json_output=True,
        )
        == 0
    )
    receipt = json.loads(capsys.readouterr().out)
    path = tmp_path / ".brigade" / "work" / "verify-runs" / receipt["run_id"] / "receipt.json"
    tampered = json.loads(path.read_text())
    tampered["status"] = "failed"
    tampered["digests"]["receipt_sha256"] = _receipt_digest(tampered)
    localio.write_json(path, tampered)

    assert receipts_cmd.verify(target=tmp_path, json_output=True) == 1
    payload = json.loads(capsys.readouterr().out)

    assert payload["summary"]["mismatch"] == 1
    problem = [item for item in payload["artifacts"] if item["status"] == "SIGNATURE-MISMATCH"][0]
    assert problem["artifact_type"] == "work-verify-receipt"
    assert problem["check"] == "digest_signature"


def test_receipts_verify_reports_foreign_key_id_as_unverifiable_without_failing(tmp_path, capsys):
    _init_git_repo(tmp_path)
    receipt_signing.generate_key(tmp_path)
    assert (
        work_cmd.verify_run(
            target=tmp_path,
            commands=["python3 -c \"print('ok')\""],
            timeout=30,
            json_output=True,
        )
        == 0
    )
    capsys.readouterr()
    receipt_signing.generate_key(tmp_path, force=True)

    assert receipts_cmd.verify(target=tmp_path, json_output=True) == 0
    payload = json.loads(capsys.readouterr().out)

    unverifiable = [item for item in payload["artifacts"] if item["status"] == "UNVERIFIABLE-SIGNATURE"]
    assert len(unverifiable) == 1
    assert unverifiable[0]["check"] == "digest_signature"
    assert "foreign key_id" in unverifiable[0]["detail"]


def test_receipts_verify_no_key_receipt_keeps_digest_checks_only(tmp_path):
    _write_verify_export_receipt(
        tmp_path,
        "20260708-120000-work-verify-no-key",
        started_at="2026-07-08T12:00:00Z",
    )

    payload = receipts_cmd.verify_payload(tmp_path)

    assert payload["summary"] == {"total": 3, "ok": 3, "mismatch": 0, "missing": 0, "legacy": 0}
    assert [(item["status"], item["check"]) for item in payload["artifacts"]] == [
        ("OK", "receipt_sha256"),
        ("OK", "log_sha256"),
        ("OK", "log_sha256"),
    ]


def test_receipts_verify_reports_missing_when_referenced_log_is_deleted(tmp_path, capsys):
    _init_git_repo(tmp_path)
    assert (
        work_cmd.verify_run(
            target=tmp_path,
            commands=["python3 -c \"print('ok')\""],
            timeout=30,
            json_output=True,
        )
        == 0
    )
    receipt = json.loads(capsys.readouterr().out)
    run_dir = tmp_path / ".brigade" / "work" / "verify-runs" / receipt["run_id"]
    (run_dir / "command-1-stdout.log").unlink()

    assert receipts_cmd.verify(target=tmp_path, json_output=True) == 1
    payload = json.loads(capsys.readouterr().out)

    assert payload["summary"]["missing"] == 1
    problem = [item for item in payload["artifacts"] if item["status"] == "MISSING"][0]
    assert problem["artifact_type"] == "work-verify-log"
    assert problem["artifact_id"].endswith("command-1-stdout.log")


def test_receipts_verify_reports_mismatch_when_middle_ledger_record_is_edited(tmp_path, capsys):
    _append_outcome(tmp_path, "ref1")
    _append_outcome(tmp_path, "ref2")
    _append_outcome(tmp_path, "ref3")
    path = tmp_path / "memory" / "outcome" / "records.jsonl"
    rows = [json.loads(line) for line in path.read_text().splitlines()]
    rows[1]["signal_value"] = -1
    path.write_text("\n".join(json.dumps(row, sort_keys=True) for row in rows) + "\n")

    assert receipts_cmd.verify(target=tmp_path, json_output=True) == 1
    payload = json.loads(capsys.readouterr().out)

    problem = [item for item in payload["artifacts"] if item["status"] == "MISMATCH"][0]
    assert problem["artifact_type"] == "outcome-ledger-record"
    assert problem["artifact_id"].endswith("records.jsonl:2")
    assert problem["check"] == "digest"


def test_receipts_verify_reports_mismatch_when_middle_ledger_record_is_deleted(tmp_path, capsys):
    _append_outcome(tmp_path, "ref1")
    _append_outcome(tmp_path, "ref2")
    _append_outcome(tmp_path, "ref3")
    path = tmp_path / "memory" / "outcome" / "records.jsonl"
    rows = path.read_text().splitlines()
    path.write_text(rows[0] + "\n" + rows[2] + "\n")

    assert receipts_cmd.verify(target=tmp_path, json_output=True) == 1
    payload = json.loads(capsys.readouterr().out)

    problem = [item for item in payload["artifacts"] if item["status"] == "MISMATCH"][0]
    assert problem["artifact_type"] == "outcome-ledger-record"
    assert problem["artifact_id"].endswith("records.jsonl:2")
    assert problem["check"] == "prev_digest"


def test_receipts_verify_reports_legacy_and_exits_zero(tmp_path, capsys):
    _write_digestless_work_receipt(tmp_path)
    legacy = {
        "artifact_id": "taste",
        "artifact_kind": "skill",
        "task_id": "legacy",
        "source": "verify",
        "signal_value": 1,
        "evidence_ref": "legacy",
        "ts": "2026-07-08T00:00:00+00:00",
    }
    path = tmp_path / "memory" / "outcome" / "records.jsonl"
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps(legacy, sort_keys=True) + "\n")

    assert receipts_cmd.verify(target=tmp_path, json_output=False) == 0
    out = capsys.readouterr().out

    assert "legacy=2" in out
    assert "LEGACY" in out


def test_doctor_and_outcome_doctor_include_receipt_summary(tmp_path, capsys):
    _init_git_repo(tmp_path)
    assert (
        work_cmd.verify_run(
            target=tmp_path,
            commands=["python3 -c \"print('ok')\""],
            timeout=30,
            json_output=True,
        )
        == 0
    )
    receipt = json.loads(capsys.readouterr().out)
    path = tmp_path / ".brigade" / "work" / "verify-runs" / receipt["run_id"] / "receipt.json"
    tampered = json.loads(path.read_text())
    tampered["status"] = "failed"
    localio.write_json(path, tampered)

    assert cli.main(["doctor", "--target", str(tmp_path)]) == 1
    doctor_out = capsys.readouterr().out
    assert "receipts: verify" in doctor_out
    assert "mismatch=1" in doctor_out

    assert cli.main(["outcome", "doctor", "--target", str(tmp_path)]) == 0
    outcome_out = capsys.readouterr().out
    assert "outcome doctor:" in outcome_out
    assert "mismatch=1" in outcome_out


def test_receipts_verify_reports_mismatch_when_referenced_log_is_edited(tmp_path, capsys):
    _init_git_repo(tmp_path)
    assert (
        work_cmd.verify_run(
            target=tmp_path,
            commands=["python3 -c \"print('ok')\""],
            timeout=30,
            json_output=True,
        )
        == 0
    )
    receipt = json.loads(capsys.readouterr().out)
    run_dir = tmp_path / ".brigade" / "work" / "verify-runs" / receipt["run_id"]
    log_path = run_dir / "command-1-stdout.log"
    log_path.write_text(log_path.read_text() + "tampered evidence\n")

    assert receipts_cmd.verify(target=tmp_path, json_output=True) == 1
    payload = json.loads(capsys.readouterr().out)

    assert payload["summary"]["mismatch"] == 1
    problem = [item for item in payload["artifacts"] if item["status"] == "MISMATCH"][0]
    assert problem["artifact_type"] == "work-verify-log"
    assert problem["artifact_id"].endswith("command-1-stdout.log")
    assert problem["check"] == "log_sha256"


def test_receipts_verify_reports_mismatch_when_graph_delta_sidecar_is_edited(tmp_path, capsys):
    _init_git_repo(tmp_path)
    run_dir = tmp_path / ".brigade" / "work" / "verify-runs" / "with-graph-delta"
    run_dir.mkdir(parents=True)
    (run_dir / "command-1-stdout.log").write_text("ok\n")
    (run_dir / "command-1-stderr.log").write_text("")
    sidecar = run_dir / "graph-delta.json"
    sidecar.write_text(json.dumps({"status": "ok", "summary": "edge_churn=0"}, sort_keys=True) + "\n")
    receipt = {
        "run_id": "with-graph-delta",
        "target": str(tmp_path),
        "status": "completed",
        "commands": [
            {
                "command": "python3 -c \"print('ok')\"",
                "status": "completed",
                "exit_code": 0,
                "stdout_log_path": str(run_dir / "command-1-stdout.log"),
                "stderr_log_path": str(run_dir / "command-1-stderr.log"),
            }
        ],
        "code_graph_delta": {"status": "ok", "summary": "edge_churn=0"},
    }
    receipt["digests"] = {
        "algorithm": "sha256",
        "logs": {
            "command-1-stderr.log": localio.file_sha256(run_dir / "command-1-stderr.log"),
            "command-1-stdout.log": localio.file_sha256(run_dir / "command-1-stdout.log"),
            "graph-delta.json": localio.file_sha256(sidecar),
        },
        "receipt_sha256": localio.canonical_json_digest(receipt, exclude_keys={"digests"}),
    }
    localio.write_json(run_dir / "receipt.json", receipt)
    sidecar.write_text(json.dumps({"status": "ok", "summary": "tampered"}, sort_keys=True) + "\n")

    assert receipts_cmd.verify(target=tmp_path, json_output=True) == 1
    payload = json.loads(capsys.readouterr().out)

    assert payload["summary"]["mismatch"] == 1
    problem = [item for item in payload["artifacts"] if item["status"] == "MISMATCH"][0]
    assert problem["artifact_type"] == "work-verify-log"
    assert problem["artifact_id"].endswith("graph-delta.json")
    assert problem["check"] == "log_sha256"
